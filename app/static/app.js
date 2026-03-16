/**
 * Math Problem Similarity A/B Comparison Dashboard
 * app.js — Frontend logic
 */

'use strict';

// ─── State ────────────────────────────────────────────────
const state = {
  currentResults: { legacy: [], improved: [], reranked: [] },
  queryProblemId: null,
  // Modal context: which card opened the modal
  modalContext: { resultId: null, searchType: null, cardEl: null },
  evaluations: new Map(), // key: `${searchType}:${resultId}` → bool
};

// ─── DOM refs ─────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

const elSpinner      = $('spinner');
const elSpinnerText  = $('spinner-text');
const elErrorBanner  = $('error-banner');
const elErrorMsg     = $('error-msg');
const elToastCont    = $('toast-container');
const elModalOverlay = $('modal-overlay');

// ─── Utility: KaTeX rendering ─────────────────────────────
function renderKaTeX(el) {
  if (!el) return;
  const tryRender = () => {
    if (typeof renderMathInElement !== 'undefined') {
      renderMathInElement(el, {
        delimiters: [
          { left: '$$',  right: '$$',  display: true  },
          { left: '$',   right: '$',   display: false },
          { left: '\\[', right: '\\]', display: true  },
          { left: '\\(', right: '\\)', display: false },
        ],
        throwOnError: false,
      });
    } else {
      setTimeout(tryRender, 100);
    }
  };
  tryRender();
}

// ─── Utility: update LaTeX preview for input textareas ───
function updateInputPreviews() {
  const qText = $('input-question').value.trim();
  const sText = $('input-solution').value.trim();
  const previewQ = $('preview-question');
  const previewS = $('preview-solution');

  if (qText) {
    previewQ.textContent = qText;
    previewQ.classList.add('visible');
    renderKaTeX(previewQ);
  } else {
    previewQ.classList.remove('visible');
    previewQ.textContent = '';
  }

  if (sText) {
    previewS.textContent = sText;
    previewS.classList.add('visible');
    renderKaTeX(previewS);
  } else {
    previewS.classList.remove('visible');
    previewS.textContent = '';
  }
}

// ─── Utility: text helpers ────────────────────────────────
function truncateText(text, maxLen = 120) {
  if (!text) return '';
  const plain = stripHtml(text);
  if (plain.length <= maxLen) return plain;
  return plain.slice(0, maxLen) + '…';
}

function stripHtml(html) {
  if (!html) return '';
  const tmp = document.createElement('div');
  tmp.innerHTML = html;
  return (tmp.textContent || tmp.innerText || '').replace(/\s+/g, ' ').trim();
}

// ─── Utility: score badge class ───────────────────────────
function scoreBadgeClass(score) {
  if (score >= 0.8) return 'score-high';
  if (score >= 0.6) return 'score-mid';
  return 'score-low';
}

// ─── Utility: grade/level badge text ─────────────────────
function gradeBadge(grade) {
  if (!grade && grade !== 0) return null;
  return `${grade}학년`;
}

function schoolBadge(level) {
  if (!level) return null;
  return { middle: '중학교', high: '고등학교' }[level] || level;
}

// ─── Toast ────────────────────────────────────────────────
function showToast(msg, type = 'info', duration = 2800) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  elToastCont.appendChild(el);
  setTimeout(() => {
    el.classList.add('toast-out');
    setTimeout(() => el.remove(), 250);
  }, duration);
}

// ─── Spinner ──────────────────────────────────────────────
function showSpinner(msg = '검색 중...') {
  elSpinnerText.textContent = msg;
  elSpinner.classList.add('visible');
}
function hideSpinner() {
  elSpinner.classList.remove('visible');
}

// ─── Error banner ─────────────────────────────────────────
function showError(msg) {
  elErrorMsg.textContent = msg;
  elErrorBanner.classList.add('visible');
}
function hideError() {
  elErrorBanner.classList.remove('visible');
}

// ─── API helpers ──────────────────────────────────────────
async function apiFetch(url, options = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return res.json();
}

// ─── Load random problem ──────────────────────────────────
async function loadRandomProblem() {
  showSpinner('랜덤 문제 불러오는 중...');
  hideError();
  try {
    const problem = await apiFetch('/api/problem/random');
    if (problem.error) {
      showToast('문제를 찾을 수 없습니다.', 'error');
      return;
    }
    $('input-problem-id').value = problem.id;
    $('input-question').value = stripHtml(problem.question_text || problem.question || '');
    $('input-solution').value = stripHtml(problem.solution_text || problem.solution || '');
    state.queryProblemId = problem.id;
    updateInputPreviews();
    showToast(`랜덤 문제 #${problem.id} 불러오기 완료`, 'success');
  } catch (err) {
    showError(`문제 불러오기 실패: ${err.message}`);
  } finally {
    hideSpinner();
  }
}

// ─── Load problem by ID ───────────────────────────────────
async function loadProblemById() {
  const idVal = $('input-problem-id').value.trim();
  if (!idVal) {
    showToast('문제 ID를 입력해주세요.', 'error');
    return;
  }
  const id = parseInt(idVal, 10);
  if (isNaN(id)) {
    showToast('유효한 숫자 ID를 입력해주세요.', 'error');
    return;
  }

  showSpinner('문제 불러오는 중...');
  hideError();
  try {
    const problem = await apiFetch(`/api/problem/${id}`);
    if (problem.error) {
      showToast(`문제를 찾을 수 없습니다: ID ${id}`, 'error');
      return;
    }
    $('input-question').value = stripHtml(problem.question_text || problem.question || '');
    $('input-solution').value = stripHtml(problem.solution_text || problem.solution || '');
    state.queryProblemId = id;
    updateInputPreviews();
    showToast(`문제 #${id} 불러오기 완료`, 'success');
  } catch (err) {
    showError(`문제 불러오기 실패: ${err.message}`);
    showToast('문제 불러오기 실패', 'error');
  } finally {
    hideSpinner();
  }
}

// ─── Build search request payload ────────────────────────
function buildSearchRequest() {
  return {
    question:        $('input-question').value.trim(),
    solution:        $('input-solution').value.trim(),
    top_k:           parseInt($('top-k').value, 10),
    q_weight:        parseFloat($('q-weight').value),
    s_weight:        parseFloat($('s-weight').value),
    grade:           $('grade').value ? parseInt($('grade').value, 10) : null,
    school_level:    $('school-level').value || null,
    exclude_id:      state.queryProblemId || null,
    rerank:          $('rerank-enabled').checked,
    rerank_top_k:    parseInt($('rerank-top-k').value, 10) || 30,
    rerank_provider: $('rerank-provider').value,
  };
}

// ─── Search compare (both systems at once) ───────────────
async function searchCompare() {
  const req = buildSearchRequest();
  if (!req.question && !req.solution) {
    showToast('문제 또는 해설 텍스트를 입력해주세요.', 'error');
    showError('문제 또는 해설 텍스트를 입력해주세요.');
    return;
  }
  hideError();
  const spinnerMsg = req.rerank
    ? 'LLM Reranking 검색 중... (30초+ 소요)'
    : '두 시스템 비교 검색 중...';
  showSpinner(spinnerMsg);
  $('btn-search').disabled = true;

  try {
    const data = await apiFetch('/api/search/compare', {
      method: 'POST',
      body: JSON.stringify(req),
    });
    const legacy   = data.legacy   || [];
    const improved = data.improved || [];
    const reranked = data.reranked || [];
    const costInfo = data.cost_info || null;

    state.currentResults = { legacy, improved, reranked };

    // Show/hide reranked column based on whether reranking is enabled and results exist
    const showReranked = req.rerank;
    setRerankedColumnVisible(showReranked);

    renderResults(legacy, improved, reranked);
    renderCostInfo(costInfo);
    await loadStats();

    const parts = [`기존 ${legacy.length}건`, `신규 ${improved.length}건`];
    if (showReranked) parts.push(`Reranked ${reranked.length}건`);
    if (costInfo && costInfo.cost_krw > 0) {
      parts.push(`비용 ₩${costInfo.cost_krw.toLocaleString()}`);
    }
    if (legacy.length === 0 && improved.length === 0 && (!showReranked || reranked.length === 0)) {
      showToast('검색 결과가 없습니다.', 'info');
    } else {
      showToast(`검색 완료: ${parts.join(', ')}`, 'success');
    }
  } catch (err) {
    showError(`검색 실패: ${err.message}`);
    showToast('검색 중 오류가 발생했습니다.', 'error');
  } finally {
    hideSpinner();
    $('btn-search').disabled = false;
  }
}

// ─── Toggle reranked column visibility ───────────────────
function setRerankedColumnVisible(visible) {
  const col = $('col-reranked');
  const grid = $('results-grid');
  if (visible) {
    col.style.display = '';
    grid.classList.add('three-columns');
    // Show footer/header reranked stats
    $('foot-reranked-divider').style.display = '';
    $('foot-reranked-block').style.display = '';
    $('hdr-reranked-divider').style.display = '';
    $('hdr-reranked-pill').style.display = '';
  } else {
    col.style.display = 'none';
    grid.classList.remove('three-columns');
    $('foot-reranked-divider').style.display = 'none';
    $('foot-reranked-block').style.display = 'none';
    $('hdr-reranked-divider').style.display = 'none';
    $('hdr-reranked-pill').style.display = 'none';
  }
}

// ─── Render results into columns ─────────────────────────
function renderResults(legacy, improved, reranked = []) {
  const legacyContainer    = $('legacy-results');
  const improvedContainer  = $('improved-results');
  const rerankedContainer  = $('reranked-results');

  $('legacy-count').textContent   = legacy.length   ? `${legacy.length}건`   : '';
  $('improved-count').textContent = improved.length ? `${improved.length}건` : '';
  $('reranked-count').textContent = reranked.length ? `${reranked.length}건` : '';

  legacyContainer.innerHTML   = legacy.length   ? '' : emptyState();
  improvedContainer.innerHTML = improved.length ? '' : emptyState();
  rerankedContainer.innerHTML = reranked.length ? '' : emptyStateReranked();

  legacy.forEach((r, i) => {
    const card = renderResultCard(r, 'legacy', i + 1);
    legacyContainer.appendChild(card);
  });
  improved.forEach((r, i) => {
    const card = renderResultCard(r, 'improved', i + 1);
    improvedContainer.appendChild(card);
  });
  reranked.forEach((r, i) => {
    const card = renderResultCard(r, 'reranked', i + 1);
    rerankedContainer.appendChild(card);
  });

  // Re-render KaTeX in all columns after DOM update
  renderKaTeX(legacyContainer);
  renderKaTeX(improvedContainer);
  renderKaTeX(rerankedContainer);
}

// ─── Render cost info badge in reranked column ──────────
function renderCostInfo(costInfo) {
  const el = $('reranked-cost');
  if (!el) return;
  if (!costInfo || costInfo.cost_usd <= 0) {
    el.style.display = 'none';
    el.textContent = '';
    return;
  }
  const krw = costInfo.cost_krw.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const usd = costInfo.cost_usd.toFixed(6);
  el.textContent = `₩${krw} ($${usd}) · ${costInfo.num_calls}건`;
  el.title = `Provider: ${costInfo.provider}\nModel: ${costInfo.model}\nCalls: ${costInfo.num_calls}`;
  el.style.display = '';
}

function emptyState() {
  return `<div class="empty-state">
    <div class="empty-icon">
      <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
      </svg>
    </div>
    <div class="empty-title">검색 결과 없음</div>
    <div>문제 텍스트를 입력하고 "검색 비교"를 클릭하세요</div>
  </div>`;
}

function emptyStateReranked() {
  return `<div class="empty-state">
    <div class="empty-icon">
      <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
      </svg>
    </div>
    <div class="empty-title">검색 결과 없음</div>
    <div>LLM Reranking을 활성화하고 검색하세요</div>
  </div>`;
}

// ─── Render a single result card ─────────────────────────
function renderResultCard(result, searchType, rank) {
  const card = document.createElement('div');
  card.className = 'result-card';
  card.dataset.resultId   = result.id;
  card.dataset.searchType = searchType;

  const score = result.score ?? result.similarity ?? 0;
  const scoreStr = score.toFixed(4);
  const scoreClass = scoreBadgeClass(score);
  const isReranked = searchType === 'reranked';

  const questionText  = result.question_text  || result.question  || '';
  const solutionText  = result.solution_text  || result.solution  || '';
  const fullQ  = stripHtml(questionText);
  const fullS  = stripHtml(solutionText);

  const evalKey = `${searchType}:${result.id}`;
  const evalState = state.evaluations.get(evalKey);

  // Meta badges
  const badges = [];
  if (gradeBadge(result.grade))           badges.push(gradeBadge(result.grade));
  if (schoolBadge(result.school_level))   badges.push(schoolBadge(result.school_level));
  if (result.source_name)                 badges.push(result.source_name);
  if (result.year)                        badges.push(`${result.year}년`);

  const badgesHtml = badges.map(b => `<span class="meta-badge">${escapeHtml(b)}</span>`).join('');

  // Tags (tag_ids is a comma-separated string or array)
  let tagsHtml = '';
  if (result.tag_ids) {
    const tagList = Array.isArray(result.tag_ids)
      ? result.tag_ids
      : String(result.tag_ids).split(',').map(t => t.trim()).filter(Boolean);
    if (tagList.length > 0) {
      const shown = tagList.slice(0, 4);
      tagsHtml = `<div style="margin-top:4px;">${shown.map(t => `<span class="meta-badge" style="background:var(--blue-50);border-color:var(--blue-100);color:var(--blue-700);">#${escapeHtml(t)}</span>`).join(' ')}${tagList.length > 4 ? ` <span class="meta-badge">+${tagList.length - 4}</span>` : ''}</div>`;
    }
  }

  const similarActive    = evalState === true  ? 'active' : '';
  const dissimilarActive = evalState === false ? 'active' : '';

  if (evalState === true)  card.classList.add('evaluated-similar');
  if (evalState === false) card.classList.add('evaluated-dissimilar');

  // Rerank score/reason display (for reranked type)
  const rerankScoreHtml = isReranked && result.rerank_score !== undefined ? `
    <span class="rerank-score">★ ${result.rerank_score}/10</span>` : '';
  const rerankReasonHtml = isReranked && result.rerank_reason ? `
    <div class="rerank-reason">${escapeHtml(result.rerank_reason)}</div>` : '';

  // For reranked cards, show vector score as secondary
  const vectorScoreHtml = isReranked ? `
    <span class="score-badge ${scoreClass}" style="opacity:0.65; font-size:10px;" title="벡터 유사도">${scoreStr}</span>` :
    `<span class="score-badge ${scoreClass}">${scoreStr}</span>`;

  card.innerHTML = `
    <div class="card-top">
      <div class="card-rank-score">
        <span class="rank-badge">${rank}</span>
        ${rerankScoreHtml || vectorScoreHtml}
        ${!isReranked && result.question_score !== undefined ? `
        <div class="score-breakdown">
          <span class="q-score">Q: ${result.question_score}</span>
          <span class="s-score">S: ${result.solution_score}</span>
        </div>` : ''}
        ${isReranked && rerankScoreHtml ? vectorScoreHtml : ''}
      </div>
      <div class="card-meta">
        <span class="problem-id">#${result.id}</span>
        ${badgesHtml}
      </div>
    </div>
    ${rerankReasonHtml}
    ${tagsHtml}
    <div class="card-text" style="margin-top:${(rerankReasonHtml || tagsHtml) ? '8px' : '0'};">
      <div class="text-label">문제</div>
      <div class="text-content">${escapeHtml(fullQ)}</div>
    </div>
    ${fullS ? `
    <div class="card-text">
      <div class="text-label">해설</div>
      <div class="text-content">${escapeHtml(fullS)}</div>
    </div>` : ''}
    <div class="card-actions">
      <span class="eval-label">유사 여부 평가</span>
      <button class="btn btn-similar ${similarActive}" data-eval="true">유사</button>
      <button class="btn btn-dissimilar ${dissimilarActive}" data-eval="false">비유사</button>
    </div>
  `;

  // Card click → open modal (but not when clicking eval buttons or expand)
  card.addEventListener('click', (e) => {
    if (e.target.closest('.btn-similar, .btn-dissimilar')) return;
    openModal(result, searchType);
  });

  // Eval button handlers
  card.querySelectorAll('[data-eval]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isSimilar = btn.dataset.eval === 'true';
      evaluate(result.id, isSimilar, searchType, card);
    });
  });

  return card;
}

// ─── Evaluate (submit rating) ─────────────────────────────
async function evaluate(resultProblemId, isSimilar, searchType, cardEl) {
  const evalKey = `${searchType}:${resultProblemId}`;
  state.evaluations.set(evalKey, isSimilar);

  // Update card UI immediately
  if (cardEl) {
    cardEl.classList.remove('evaluated-similar', 'evaluated-dissimilar');
    cardEl.classList.add(isSimilar ? 'evaluated-similar' : 'evaluated-dissimilar');
    cardEl.querySelectorAll('.btn-similar, .btn-dissimilar').forEach(b => b.classList.remove('active'));
    const activeBtn = cardEl.querySelector(isSimilar ? '.btn-similar' : '.btn-dissimilar');
    if (activeBtn) activeBtn.classList.add('active');
  }

  // Close modal if it was triggered from modal
  if (state.modalContext.resultId === resultProblemId && state.modalContext.searchType === searchType) {
    closeModal();
  }

  try {
    await apiFetch('/api/evaluate', {
      method: 'POST',
      body: JSON.stringify({
        query_problem_id: state.queryProblemId ?? null,
        result_problem_id: resultProblemId,
        is_similar: isSimilar,
        search_type: searchType,
      }),
    });
    const typeLabel = searchType === 'legacy' ? '기존' : searchType === 'improved' ? '신규' : 'LLM Reranking';
    showToast(
      `[${typeLabel}] #${resultProblemId} → ${isSimilar ? '유사' : '비유사'} 저장`,
      'success',
      2000
    );
    await loadStats();
  } catch (err) {
    showToast('평가 저장 실패: ' + err.message, 'error');
  }
}

// ─── Load and render stats ────────────────────────────────
async function loadStats() {
  try {
    const data = await apiFetch('/api/stats');
    const total        = data.total ?? 0;
    const legacyPrec   = data.legacy?.precision  ?? null;
    const improvedPrec = data.improved?.precision ?? null;
    const legacyTotal  = data.legacy?.total       ?? 0;
    const improvedTotal= data.improved?.total     ?? 0;
    const legacySim    = data.legacy?.similar     ?? 0;
    const improvedSim  = data.improved?.similar   ?? 0;

    const fmtPrec = (p) => p === null ? '—' : (p * 100).toFixed(1) + '%';

    // Header
    $('hdr-total').textContent        = total > 0 ? `${total}건` : '—';
    $('hdr-legacy-prec').textContent  = fmtPrec(legacyPrec);
    $('hdr-improved-prec').textContent= fmtPrec(improvedPrec);

    // Footer
    $('foot-total').textContent         = total > 0 ? total : '—';
    $('foot-legacy-prec').textContent   = fmtPrec(legacyPrec);
    $('foot-improved-prec').textContent = fmtPrec(improvedPrec);

    if (legacyTotal > 0) {
      $('foot-legacy-detail').textContent = `유사 ${legacySim} / ${legacyTotal}건`;
    }
    if (improvedTotal > 0) {
      $('foot-improved-detail').textContent = `유사 ${improvedSim} / ${improvedTotal}건`;
    }
  } catch {
    // Stats load failure is non-critical — silently ignore
  }
}

// ─── Modal ────────────────────────────────────────────────
function openModal(problem, searchType) {
  state.modalContext = {
    resultId:   problem.id,
    searchType: searchType,
    cardEl:     document.querySelector(
      `.result-card[data-result-id="${problem.id}"][data-search-type="${searchType}"]`
    ),
  };

  const typeLabelMap = { legacy: '기존', improved: '신규', reranked: 'LLM Reranking' };
  const typeColorMap = {
    legacy:   { bg: 'var(--blue-50)', color: 'var(--blue-700)', border: 'var(--blue-100)' },
    improved: { bg: 'var(--green-50)', color: 'var(--green-700)', border: 'var(--green-100)' },
    reranked: { bg: 'var(--purple-50, #f5f3ff)', color: 'var(--purple-700, #6d28d9)', border: 'var(--purple-100, #ede9fe)' },
  };
  const colors = typeColorMap[searchType] || typeColorMap.legacy;

  // Type badge
  const badge = $('modal-type-badge');
  badge.textContent = typeLabelMap[searchType] || searchType;
  badge.style.cssText = `background:${colors.bg};color:${colors.color};border-color:${colors.border};`;

  $('modal-problem-id').textContent = `#${problem.id}`;

  // Meta row
  const metaBadges = [];
  if (gradeBadge(problem.grade))          metaBadges.push(gradeBadge(problem.grade));
  if (schoolBadge(problem.school_level))  metaBadges.push(schoolBadge(problem.school_level));
  if (problem.source_name)                metaBadges.push(problem.source_name);
  if (problem.year)                       metaBadges.push(`${problem.year}년`);
  if (problem.exam_type)                  metaBadges.push(problem.exam_type);

  const score = problem.score ?? problem.similarity;
  if (score !== undefined && score !== null) {
    metaBadges.push(`유사도: ${Number(score).toFixed(4)}`);
  }

  $('modal-meta-row').innerHTML = metaBadges
    .map(b => `<span class="meta-badge">${escapeHtml(String(b))}</span>`)
    .join('');

  // Question / Refer / Solution
  const questionText = problem.question_text || problem.question || '(없음)';
  const referText    = problem.refer || '';
  const solutionText = problem.solution_text || problem.solution || '';

  const modalQuestion = $('modal-question');
  modalQuestion.innerHTML = escapeHtml(stripHtml(questionText));
  renderKaTeX(modalQuestion);

  const referSection = $('modal-refer-section');
  if (referText) {
    referSection.style.display = '';
    const modalRefer = $('modal-refer');
    modalRefer.innerHTML = escapeHtml(stripHtml(referText));
    renderKaTeX(modalRefer);
  } else {
    referSection.style.display = 'none';
  }

  const solutionSection = $('modal-solution-section');
  if (solutionText) {
    solutionSection.style.display = '';
    const modalSolution = $('modal-solution');
    modalSolution.innerHTML = escapeHtml(stripHtml(solutionText));
    renderKaTeX(modalSolution);
  } else {
    solutionSection.style.display = 'none';
  }

  // Modal eval buttons context
  const evalKey = `${searchType}:${problem.id}`;
  const evalState = state.evaluations.get(evalKey);
  const btnSim    = $('modal-btn-similar');
  const btnDis    = $('modal-btn-dissimilar');
  btnSim.classList.toggle('active', evalState === true);
  btnDis.classList.toggle('active', evalState === false);

  elModalOverlay.classList.add('visible');
  document.body.style.overflow = 'hidden';
}

function closeModal() {
  elModalOverlay.classList.remove('visible');
  document.body.style.overflow = '';
  state.modalContext = { resultId: null, searchType: null, cardEl: null };
}

// ─── HTML escape helpers ──────────────────────────────────
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escapeAttr(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ─── Weight slider sync ───────────────────────────────────
function setupWeightSliders() {
  const qSlider = $('q-weight');
  const sSlider = $('s-weight');
  const qVal    = $('q-weight-val');
  const sVal    = $('s-weight-val');

  qSlider.addEventListener('input', () => {
    const q = parseFloat(qSlider.value);
    const s = Math.round((1 - q) * 100) / 100;
    sSlider.value  = s;
    qVal.textContent = q.toFixed(2);
    sVal.textContent = s.toFixed(2);
  });
}

// ─── Keyboard shortcuts ───────────────────────────────────
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (elModalOverlay.classList.contains('visible')) closeModal();
  }
  // Ctrl/Cmd + Enter → search
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    if (!elModalOverlay.classList.contains('visible')) searchCompare();
  }
});

// ─── Event wiring ─────────────────────────────────────────
$('btn-search').addEventListener('click', searchCompare);
$('btn-load').addEventListener('click', loadProblemById);
$('btn-random').addEventListener('click', loadRandomProblem);

// Load on Enter in ID field
$('input-problem-id').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadProblemById();
});

// Modal close
$('modal-close').addEventListener('click', closeModal);
$('modal-close-btn').addEventListener('click', closeModal);
elModalOverlay.addEventListener('click', (e) => {
  if (e.target === elModalOverlay) closeModal();
});

// Modal eval buttons
$('modal-btn-similar').addEventListener('click', () => {
  const { resultId, searchType, cardEl } = state.modalContext;
  if (resultId !== null) evaluate(resultId, true, searchType, cardEl);
});
$('modal-btn-dissimilar').addEventListener('click', () => {
  const { resultId, searchType, cardEl } = state.modalContext;
  if (resultId !== null) evaluate(resultId, false, searchType, cardEl);
});

// ─── Rerank checkbox toggle ───────────────────────────────
$('rerank-enabled').addEventListener('change', () => {
  const enabled = $('rerank-enabled').checked;
  $('rerank-options').style.display  = enabled ? '' : 'none';
  $('rerank-topk-item').style.display = enabled ? '' : 'none';
  if (!enabled) {
    // Hide reranked column when toggled off
    setRerankedColumnVisible(false);
  }
});

// ─── Init ─────────────────────────────────────────────────
setupWeightSliders();
loadStats();
