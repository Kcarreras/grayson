/* Criteria drafts stay local to the form until saved; browsing never executes SQL. */
(function () {
  const form = document.getElementById('criteria-form');
  if (!form) return;
  const list = document.getElementById('criterion-list');
  const cache = new Map();
  const key = q => q.session_id + '::' + q.qid;
  JSON.parse(document.getElementById('criteria-query-data').textContent).forEach(q => cache.set(key(q), q));
  const state = new WeakMap();
  const field = (row, name) => row.querySelector('[name="' + name + '"]');
  const review = document.getElementById('criteria-approval');
  const approveButton = review && review.querySelector('button');
  const scopeBlocked = approveButton && approveButton.disabled;
  const formState = () => JSON.stringify(Array.from(new FormData(form).entries()));
  const initial = formState();

  function updateApproval() {
    if (!review) return;
    const dirty = formState() !== initial;
    approveButton.disabled = scopeBlocked || dirty;
    document.getElementById('criteria-unsaved').hidden = !dirty;
  }
  if (review) review.addEventListener('submit', e => {
    if (formState() !== initial) { e.preventDefault(); updateApproval(); }
  });

  function rules(row) {
    const scalar = field(row, 'kind').value === 'scalar';
    const tolerance = field(row, 'relative_percent').value.trim() !== '';
    row.querySelectorAll('[data-scalar]').forEach(el => el.hidden = !scalar);
    row.querySelectorAll('[data-bounds]').forEach(el => el.hidden = !scalar || tolerance);
    row.querySelector('[data-upper]').hidden = !scalar || tolerance || field(row, 'operator').value !== 'between';
    field(row, 'column').required = scalar;
  }

  function preview(row) {
    const q = cache.get(field(row, 'source_qid').value);
    row.querySelector('.selected-query').hidden = !q;
    if (!q) return;
    row.querySelector('.selected-meta').textContent = (q.current ? 'Current session' : q.session_title + ' · ' + q.session_id) + ' · ' + q.qid + ' · ' + q.ts;
    row.querySelector('.selected-scope').textContent = q.scope_required.length
      ? 'Scope approval needed: ' + q.scope_required.join(', ') + '. Saving will request your approval.'
      : 'Tables in approved scope: ' + q.tables.join(', ');
    row.querySelector('.query-preview').textContent = q.sql;
    const url = new URL('/session/' + encodeURIComponent(q.session_id) + '/query/' + encodeURIComponent(q.qid), location.origin);
    url.search = new URL(form.dataset.queryUrl, location.origin).search;
    row.querySelector('.query-link').href = url.href;
  }

  function renderOptions(row, queries) {
    const select = field(row, 'source_qid');
    const selected = select.value;
    const previous = select.selectedOptions[0];
    select.replaceChildren(new Option('Choose an executed query…', ''));
    // Keep an existing selection visible while searching or changing pages.
    if (selected && !queries.some(q => key(q) === selected)) {
      const q = cache.get(selected);
      const label = q ? q.qid + ' · ' + q.label : previous.textContent;
      select.add(new Option('Selected · ' + label, selected));
    }
    const groups = new Map();
    queries.forEach(q => {
      cache.set(key(q), q);
      if (!groups.has(q.session_id)) {
        const group = document.createElement('optgroup');
        group.label = q.current ? 'Current session' : q.session_title + ' · ' + q.session_id;
        groups.set(q.session_id, group);
        select.appendChild(group);
      }
      const option = new Option(q.qid + ' · ' + q.label + (q.scope_required.length ? ' · scope approval needed' : ''), key(q));
      groups.get(q.session_id).appendChild(option);
    });
    select.value = selected;
    preview(row);
  }

  async function load(row, append = false) {
    const data = state.get(row);
    if (data.controller) data.controller.abort();
    const controller = new AbortController();
    data.controller = controller;
    const status = row.querySelector('.query-status');
    const more = row.querySelector('.more-queries');
    const select = field(row, 'source_qid');
    status.textContent = 'Loading queries…';
    select.setAttribute('aria-busy', 'true');
    more.hidden = true;
    const url = new URL(form.dataset.queryUrl, location.origin);
    url.searchParams.set('source_session', row.querySelector('.query-session').value);
    url.searchParams.set('search', row.querySelector('.query-search').value);
    url.searchParams.set('offset', append ? data.next : 0);
    try {
      const response = await fetch(url, {credentials: 'same-origin', signal: controller.signal});
      if (!response.ok) throw new Error('Query search failed. Change the search or session to retry.');
      const result = await response.json();
      if (data.controller !== controller || !row.isConnected) return;
      data.queries = append ? data.queries.concat(result.queries) : result.queries;
      data.next = result.next_offset;
      renderOptions(row, data.queries);
      status.textContent = data.queries.length ? data.queries.length + (data.queries.length === 1 ? ' query shown' : ' queries shown') : 'No executed queries match this search.';
      more.hidden = data.next === null;
    } catch (e) {
      if (e.name !== 'AbortError') status.textContent = 'Unable to load queries. Change the search or session to retry.';
    } finally {
      if (data.controller === controller) select.removeAttribute('aria-busy');
    }
  }

  function init(row) {
    state.set(row, {queries: [], next: null, controller: null, timer: null});
    rules(row);
    preview(row);
    load(row);
  }

  // Cloned fields also need help to reopen after Escape dismisses it.
  ['mouseover', 'focusin'].forEach(name => list.addEventListener(name, e => {
    const help = e.target.closest('.help');
    if (help) help.classList.remove('help-dismissed');
  }));

  list.addEventListener('input', e => {
    const row = e.target.closest('.criterion-row');
    if (!row) return;
    rules(row);
    updateApproval();
    if (e.target.classList.contains('query-search')) {
      const data = state.get(row);
      clearTimeout(data.timer);
      if (data.controller) data.controller.abort();
      data.timer = setTimeout(() => load(row), 200);
    }
  });
  list.addEventListener('change', e => {
    const row = e.target.closest('.criterion-row');
    if (!row) return;
    rules(row);
    updateApproval();
    if (e.target.classList.contains('query-session')) {
      clearTimeout(state.get(row).timer);
      load(row);
    }
    if (e.target.name === 'source_qid') preview(row);
  });
  list.addEventListener('click', e => {
    const row = e.target.closest('.criterion-row');
    if (!row) return;
    if (e.target.classList.contains('more-queries')) load(row, true);
    if (e.target.classList.contains('remove-criterion') && list.children.length > 1) {
      const data = state.get(row);
      clearTimeout(data.timer);
      if (data.controller) data.controller.abort();
      row.remove();
      updateApproval();
    }
  });
  document.getElementById('add-criterion').addEventListener('click', () => {
    const row = list.firstElementChild.cloneNode(true);
    row.querySelectorAll('input').forEach(i => i.value = i.name === 'value' ? '0' : '');
    field(row, 'kind').value = 'scalar';
    field(row, 'operator').value = 'eq';
    field(row, 'source_qid').replaceChildren(new Option('Choose an executed query…', ''));
    row.querySelector('.query-session').value = form.dataset.sessionId;
    row.querySelectorAll('details').forEach(el => el.open = false);
    const used = new Set(Array.from(list.querySelectorAll('[name="criterion_id"]'), el => el.value));
    let number = 1;
    while (used.has('criterion_' + number)) number++;
    field(row, 'criterion_id').value = 'criterion_' + number;
    list.appendChild(row);
    updateApproval();
    init(row);
    field(row, 'name').focus();
  });
  list.querySelectorAll('.criterion-row').forEach(init);
})();
