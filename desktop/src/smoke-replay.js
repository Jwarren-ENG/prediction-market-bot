// Run only in an explicitly requested, isolated offline native acceptance test.
(() => {
  const root = document.documentElement;
  const replay = document.querySelector('button[data-action="replay"]:not(:disabled)');
  if (replay && !root.dataset.pmbSmokeReplay) {
    root.dataset.pmbSmokeReplay = 'requested';
    replay.click();
  }
  const accepted = document.querySelector('#notices [data-key="success"] span')?.textContent;
  if (accepted !== 'Offline replay processed.') return null;
  return {
    heading: document.querySelector('main h1')?.textContent || '',
    rows: document.querySelectorAll('main table tbody tr').length,
    nav: document.querySelector('nav')?.innerText || '',
    replay_posted: true
  };
})()
