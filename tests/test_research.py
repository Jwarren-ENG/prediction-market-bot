import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from worker.research import Research, analyze, evidence_packet, route_url
from worker.storage import Store

SOURCE = {
    "title": "Forecast issued",
    "url": "https://api.weather.gov/gridpoints/OKX/1,1/forecast",
    "published_at": "2026-11-20T07:00:00Z",
    "available_at": "2026-11-20T07:01:00Z",
    "detail": "Issued forecast maximum 51 F.",
}
MARKET = {
    "id": "fixture-test",
    "event_date": "2026-11-20",
    "as_of": "2026-11-20T12:00:00Z",
    "evidence": [SOURCE],
    "levels": {"yes": [[51, 10]]},
    "owner_secret": "NEVER_SEND",
}


class AnalystTest(unittest.TestCase):
    def test_only_timely_permitted_source_data_is_exported(self):
        m = {
            **MARKET,
            "evidence": [
                SOURCE,
                {**SOURCE, "url": "https://external-api.kalshi.com/trade-api/v2"},
                {**SOURCE, "available_at": "2026-11-20T12:00:01Z"},
                {**SOURCE, "url": "https://api.weather.gov.untrusted.test/"},
                {**SOURCE, "published_at": None},
            ],
        }
        packet = evidence_packet(m, "forward")
        self.assertEqual(len(packet["sources"]), 1)
        self.assertNotIn("NEVER_SEND", str(packet))
        self.assertNotIn("levels", packet)
        self.assertNotIn(m["id"], str(packet))
        with self.assertRaises(ValueError):
            route_url("http://public-gateway.test/v1")
        self.assertEqual(
            route_url("http://127.0.0.1:20128/v1/"), "http://127.0.0.1:20128/v1"
        )

    def test_native_adapters_send_and_read_the_right_wire_format(self):
        packet = evidence_packet(MARKET, "forward")
        checked = []

        def fetch(url, headers, body):
            checked.append(url)
            if "googleapis" in url:
                self.assertEqual(headers["x-goog-api-key"], "dev")
                return {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "thinking", "thought": True},
                                    {"text": "forecast note"},
                                ]
                            }
                        }
                    ]
                }
            if "perplexity" in url:
                self.assertNotIn("tools", body)
                return {
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "critical note",
                                    "annotations": [
                                        {"url": SOURCE["url"], "title": "NWS"}
                                    ],
                                }
                            ],
                        }
                    ]
                }
            self.assertEqual(body["model"], "local/model")
            return {"choices": [{"message": {"content": "independent note"}}]}

        for p, model, note in [
            ("gemini", "example", "forecast note"),
            ("perplexity", "low-cost", "critical note"),
            ("omniroute", "local/model", "independent note"),
        ]:
            result, cites = analyze(
                p,
                {"model": model, "endpoint": "http://localhost:20128/v1"},
                "dev",
                packet,
                fetch,
            )
            self.assertEqual(result, note)
            if p == "perplexity":
                self.assertEqual(cites, [{"title": "NWS", "url": SOURCE["url"]}])
        self.assertEqual(len(checked), 3)

    def test_zero_external_calls_by_default_and_budget_retries_are_deduplicated(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(os.environ, {}, clear=True),
        ):
            research = Research(Store(Path(temp)))
            with patch("worker.research.analyze") as outbound:
                research.run(MARKET, "forward")
                outbound.assert_not_called()
            self.assertEqual(len(research.state("forward")["notes"]), 1)
            research.configure(
                {
                    "provider": "omniroute",
                    "enabled": True,
                    "model": "test",
                    "endpoint": "http://127.0.0.1:20128/v1",
                    "max_calls_per_day": 1,
                }
            )
            with patch(
                "worker.research.analyze", return_value=("model note", [])
            ) as outbound:
                research.run(MARKET, "forward")
                research.run(MARKET, "forward")
                research.run(
                    {**MARKET, "id": "second", "event_date": "2026-11-21"}, "forward"
                )
                outbound.assert_called_once()
            state = research.state("forward")
            self.assertEqual(state["reserved_this_month_cents"], 25)
            self.assertEqual(state["configured"], ["OmniRoute"])
            self.assertTrue(any(n["text"] == "model note" for n in state["notes"]))
            self.assertEqual(research.state("replay")["notes"], [])

    def test_provider_errors_never_touch_a_paper_account(self):
        with tempfile.TemporaryDirectory() as temp:
            research = Research(Store(Path(temp)))
            research.configure(
                {
                    "provider": "omniroute",
                    "enabled": True,
                    "model": "test",
                    "max_calls_per_day": 1,
                }
            )
            with patch(
                "worker.research.analyze", side_effect=ValueError("unexpected reply")
            ):
                research.run(MARKET, "forward")
            notes = research.state("forward")["notes"]
            self.assertEqual(
                [n["status"] for n in notes if n["provider"] == "OmniRoute"], ["error"]
            )

    def test_saved_and_environment_gateway_keys_are_bound_to_the_original_endpoint(
        self,
    ):
        good = "https://one.example/v1"
        other = "https://two.example/v1"
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(
                os.environ,
                {"OMNIROUTE_API_KEY": "example-env", "OMNIROUTE_BASE_URL": good},
                clear=True,
            ),
        ):
            research = Research(Store(Path(temp)))
            cfg = {
                "provider": "omniroute",
                "enabled": True,
                "model": "test",
                "endpoint": good,
            }
            with (
                patch("worker.research.save_secret"),
                patch(
                    "worker.research.get_secret", return_value="example-owner"
                ) as vault,
            ):
                research.configure({**cfg, "key": "example-owner"})
                with patch(
                    "worker.research.analyze", return_value=("done", [])
                ) as client:
                    research.run(MARKET, "forward")
                    self.assertEqual(client.call_args.args[2], "example-owner")
                    research.configure({**cfg, "endpoint": other})
                    research.run({**MARKET, "id": "next"}, "forward")
                    self.assertEqual(client.call_args.args[2], "")
                    self.assertEqual(vault.call_count, 1)
            self.assertNotIn("example-owner", str(research.state("forward")))

    def test_offline_research_bypasses_provider_even_if_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            research = Research(Store(Path(temp)))
            research.configure(
                {"provider": "omniroute", "enabled": True, "model": "test"}
            )
            with patch("worker.research.analyze") as client:
                research.run(MARKET, "forward", local_only=True)
                client.assert_not_called()
            self.assertEqual(research.state("forward")["reserved_this_month_cents"], 0)

    def test_publication_and_last_retrieval_must_both_fit_the_cutoff(self):
        items = [
            {**SOURCE, "retrieved_at": "2026-11-20T12:05:00Z"},
            {
                **SOURCE,
                "published_at": "2026-11-20T07:00:30Z",
                "available_at": "2026-11-20T07:00:20Z",
            },
            {
                **SOURCE,
                "kind": "observation",
                "published_at": None,
                "issued_at": SOURCE["published_at"],
            },
        ]
        result = evidence_packet({**MARKET, "evidence": items}, "forward")
        self.assertEqual(len(result["sources"]), 1)
        obs = result["sources"][0]
        self.assertIsNone(obs["published_at"])
        self.assertEqual(obs["observation_time"], SOURCE["published_at"])
