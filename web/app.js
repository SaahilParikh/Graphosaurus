// Thesaurus graph explorer. Fetches ego graphs from the Python server,
// renders with Cytoscape, merges on expand.
//
// State model:
//   seenNodes: Set<string>   -- words currently in the graph
//   seenEdges: Set<string>   -- undirected edge IDs "a--b" (a < b)
// On every fetch we union the response into the graph and re-layout.
//
// Same-origin fetch against the Python server; no CORS config needed.

(() => {
  'use strict';

  const API = '';  // same origin
  const seenNodes = new Set();
  const seenEdges = new Set();
  let cy;
  let searchDebounce = null;

  const el = (id) => document.getElementById(id);

  function init() {
    // Register fcose. The CDN bundle exposes the factory on window.
    if (window.cytoscapeFcose) {
      cytoscape.use(window.cytoscapeFcose);
    }

    cy = cytoscape({
      container: el('cy'),
      style: [
        {
          selector: 'node',
          style: {
            'label': 'data(id)',
            'text-valign': 'center',
            'text-halign': 'center',
            'font-size': 12,
            'background-color': '#4f46e5',
            'color': 'white',
            'text-outline-color': '#4f46e5',
            'text-outline-width': 2,
            'width': 28,
            'height': 28,
          },
        },
        {
          selector: 'node.center',
          style: {
            'background-color': '#f59e0b',
            'text-outline-color': '#f59e0b',
            'width': 40,
            'height': 40,
            'font-size': 14,
            'font-weight': 'bold',
          },
        },
        {
          selector: 'edge',
          style: {
            'width': 1.5,
            'line-color': '#9ca3af',
            'curve-style': 'bezier',
            'target-arrow-shape': 'none',
          },
        },
      ],
      minZoom: 0.1,
      maxZoom: 4,
      wheelSensitivity: 0.25,
    });

    cy.on('tap', 'node', (evt) => {
      loadNeighborhood(evt.target.id());
    });

    // Search box wiring
    el('search').addEventListener('input', onSearchInput);
    el('search').addEventListener('keydown', onSearchKeydown);
    el('search').addEventListener('blur', () => {
      // Delay so click on a suggestion still registers.
      setTimeout(() => hideSuggestions(), 150);
    });

    // Depth slider
    el('depth').addEventListener('input', () => {
      el('depthVal').textContent = el('depth').value;
    });

    // Reset
    el('reset').addEventListener('click', reset);

    setStatus('ready -- search for a word');
  }

  // --- Search / autocomplete ---

  function onSearchInput(e) {
    const q = e.target.value.trim();
    clearTimeout(searchDebounce);
    if (!q) {
      hideSuggestions();
      return;
    }
    searchDebounce = setTimeout(() => fetchSuggestions(q), 120);
  }

  async function fetchSuggestions(q) {
    try {
      const r = await fetch(`${API}/api/search?q=${encodeURIComponent(q)}&limit=12`);
      if (!r.ok) { setStatus(`search error: ${r.status}`); return; }
      const data = await r.json();
      renderSuggestions(data.matches || []);
    } catch (err) {
      setStatus(`search failed: ${err.message}`);
    }
  }

  function renderSuggestions(matches) {
    const box = el('suggestions');
    box.innerHTML = '';
    if (!matches.length) { hideSuggestions(); return; }
    matches.forEach((m, i) => {
      const d = document.createElement('div');
      d.textContent = m;
      d.setAttribute('role', 'option');
      if (i === 0) d.classList.add('active');
      // mousedown fires before blur; click would miss.
      d.addEventListener('mousedown', (ev) => {
        ev.preventDefault();
        selectWord(m);
      });
      box.appendChild(d);
    });
    box.style.display = 'block';
  }

  function hideSuggestions() {
    el('suggestions').style.display = 'none';
  }

  function selectWord(w) {
    el('search').value = w;
    hideSuggestions();
    loadNeighborhood(w);
  }

  function onSearchKeydown(e) {
    const box = el('suggestions');
    const items = Array.from(box.children);
    if (items.length === 0) {
      if (e.key === 'Enter') {
        loadNeighborhood(el('search').value.trim());
      }
      return;
    }
    const idx = items.findIndex((x) => x.classList.contains('active'));
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      move(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      move(-1);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const target = items[idx < 0 ? 0 : idx];
      if (target) selectWord(target.textContent);
    } else if (e.key === 'Escape') {
      hideSuggestions();
    }

    function move(delta) {
      if (idx >= 0) items[idx].classList.remove('active');
      const next = (idx + delta + items.length) % items.length;
      items[next].classList.add('active');
      items[next].scrollIntoView({ block: 'nearest' });
    }
  }

  // --- Neighborhood fetch + merge ---

  async function loadNeighborhood(word) {
    const w = (word || '').trim().toLowerCase();
    if (!w) return;
    const depth = parseInt(el('depth').value, 10);
    setStatus(`loading "${w}" @ depth ${depth}...`);
    try {
      const r = await fetch(
        `${API}/api/neighborhood?word=${encodeURIComponent(w)}&depth=${depth}`
      );
      if (!r.ok) {
        setStatus(`error: ${r.status}`);
        return;
      }
      const graph = await r.json();
      if (!graph.nodes || graph.nodes.length === 0) {
        setStatus(`no neighborhood for "${w}" (not in dictionary or no synonyms)`);
        return;
      }
      mergeGraph(graph, w);
      setStatus(
        `"${w}" loaded: +${countNew(graph)} new (${graph.nodes.length} nodes, ` +
        `${graph.edges.length} edges at depth ${graph.depth})`
      );
    } catch (err) {
      setStatus(`failed: ${err.message}`);
    }
  }

  function countNew(graph) {
    let n = 0;
    for (const node of graph.nodes) {
      if (!seenNodes.has(node)) n++;
    }
    return n;
  }

  function mergeGraph(graph, centerWord) {
    const toAdd = [];
    for (const node of graph.nodes) {
      if (!seenNodes.has(node)) {
        seenNodes.add(node);
        toAdd.push({ group: 'nodes', data: { id: node } });
      }
    }
    for (const [a, b] of graph.edges) {
      // Canonical undirected ID.
      const [lo, hi] = a < b ? [a, b] : [b, a];
      const id = `${lo}--${hi}`;
      if (!seenEdges.has(id)) {
        seenEdges.add(id);
        toAdd.push({ group: 'edges', data: { id, source: lo, target: hi } });
      }
    }
    cy.add(toAdd);

    // Mark the new center; unmark old centers.
    cy.nodes().removeClass('center');
    const center = cy.getElementById(centerWord);
    if (center && center.length) center.addClass('center');

    // Re-layout. fcose with randomize=false keeps existing positions
    // stable and only settles new nodes into place.
    const layout = cy.layout({
      name: window.cytoscapeFcose ? 'fcose' : 'cose',
      animate: true,
      animationDuration: 500,
      randomize: false,
      fit: false,
      padding: 40,
      nodeRepulsion: 4500,
      idealEdgeLength: 80,
    });
    layout.run();

    // Pan/zoom to put the center node in view without yanking too much.
    if (center && center.length) {
      cy.animate(
        { center: { eles: center }, zoom: Math.max(cy.zoom(), 1) },
        { duration: 500 }
      );
    }
  }

  // --- UI helpers ---

  function reset() {
    seenNodes.clear();
    seenEdges.clear();
    cy.elements().remove();
    el('search').value = '';
    hideSuggestions();
    setStatus('cleared');
  }

  function setStatus(msg) {
    el('status').textContent = msg;
  }

  init();
})();
