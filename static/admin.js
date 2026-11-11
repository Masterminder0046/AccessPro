/**
 * ASSESSPRO — Enterprise Admin Dashboard Script
 * Manages modals, bulk question parsing & preview, live monitoring polling, and audit drawers.
 */

// Global State
let parsedBulkQuestions = [];

// ==========================================================================
// MODAL MANAGEMENT
// ==========================================================================

function openModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) {
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
  }
}

function closeModal(modalId) {
  const modal = document.getElementById(modalId);
  if (modal) {
    modal.classList.remove('active');
    document.body.style.overflow = '';
  }
}

// Close modals when clicking backdrop
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-backdrop')) {
    e.target.classList.remove('active');
    document.body.style.overflow = '';
  }
});

// ESC key closes active modals
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-backdrop.active').forEach((m) => {
      m.classList.remove('active');
    });
    document.body.style.overflow = '';
  }
});

// ==========================================================================
// TOAST NOTIFICATIONS
// ==========================================================================

function showToast(message, type = 'info') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.style.cssText = 'position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; gap: 8px;';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  const bg = type === 'success' ? '#10b981' : (type === 'danger' ? '#ef4444' : '#2563eb');
  toast.style.cssText = `background: ${bg}; color: white; padding: 12px 20px; border-radius: 8px; font-weight: 600; font-size: 0.9rem; box-shadow: 0 4px 12px rgba(0,0,0,0.15); animation: toastIn 0.25s ease-out;`;
  toast.innerText = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transition = 'opacity 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// Copy to Clipboard Helper
function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => {
    showToast('Examination link copied to clipboard!', 'success');
  }).catch(() => {
    const el = document.createElement('textarea');
    el.value = text;
    document.body.appendChild(el);
    el.select();
    document.execCommand('copy');
    document.body.removeChild(el);
    showToast('Examination link copied to clipboard!', 'success');
  });
}

// ==========================================================================
// BULK QUESTION PARSER & PREVIEW ENGINE
// ==========================================================================

function parseBulkInput() {
  const textarea = document.getElementById('bulk-questions-input');
  const rawText = textarea ? textarea.value : '';
  const errorBox = document.getElementById('bulk-errors');
  const previewBox = document.getElementById('bulk-preview-container');
  const confirmBtn = document.getElementById('btn-confirm-bulk-save');

  if (!rawText.trim()) {
    showToast('Please paste questions in the textarea first.', 'danger');
    return;
  }

  // Show loading
  const parseBtn = document.getElementById('btn-parse-bulk');
  if (parseBtn) {
    parseBtn.disabled = true;
    parseBtn.innerHTML = 'Parsing Questions...';
  }

  fetch('/admin/questions/bulk-parse', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ raw_text: rawText })
  })
    .then((res) => res.json())
    .then((data) => {
      if (parseBtn) {
        parseBtn.disabled = false;
        parseBtn.innerHTML = 'Parse & Validate Questions';
      }

      parsedBulkQuestions = data.questions || [];

      // Render Errors & Warnings
      if (errorBox) {
        errorBox.innerHTML = '';
        if (data.errors && data.errors.length > 0) {
          errorBox.style.display = 'block';
          let html = '<div class="alert alert-danger" style="margin-bottom: 12px;"><strong>Parsing Warnings / Errors:</strong><ul style="margin-left: 20px; margin-top: 6px;">';
          data.errors.forEach((err) => { html += `<li>${err}</li>`; });
          html += '</ul></div>';
          errorBox.innerHTML = html;
        } else {
          errorBox.style.display = 'none';
        }
      }

      // Render Preview Table
      renderParsedPreview();

      if (confirmBtn) {
        confirmBtn.disabled = parsedBulkQuestions.length === 0;
      }

      if (parsedBulkQuestions.length > 0) {
        showToast(`Parsed ${parsedBulkQuestions.length} valid question(s)! Review preview below.`, 'success');
      } else {
        showToast('No valid questions parsed. Please check the format guidelines.', 'danger');
      }
    })
    .catch((err) => {
      if (parseBtn) {
        parseBtn.disabled = false;
        parseBtn.innerHTML = 'Parse & Validate Questions';
      }
      showToast('Error communicating with server: ' + err, 'danger');
    });
}

function handleCsvFileSelect(e) {
  const file = e.target.files[0];
  if (!file) return;

  const formData = new FormData();
  formData.append('csv_file', file);

  showToast('Parsing CSV file...', 'info');

  fetch('/admin/questions/bulk-parse', {
    method: 'POST',
    body: formData
  })
    .then((res) => res.json())
    .then((data) => {
      parsedBulkQuestions = data.questions || [];
      renderParsedPreview();
      const confirmBtn = document.getElementById('btn-confirm-bulk-save');
      if (confirmBtn) confirmBtn.disabled = parsedBulkQuestions.length === 0;
      showToast(`Parsed ${parsedBulkQuestions.length} questions from CSV.`, 'success');
    })
    .catch((err) => showToast('Failed to parse CSV file: ' + err, 'danger'));
}

function renderParsedPreview() {
  const container = document.getElementById('bulk-preview-list');
  const countBadge = document.getElementById('parsed-count-badge');
  if (countBadge) countBadge.innerText = `${parsedBulkQuestions.length} questions`;

  if (!container) return;

  if (parsedBulkQuestions.length === 0) {
    container.innerHTML = '<div style="padding: 24px; text-align: center; color: var(--text-muted);">No questions parsed yet. Paste questions above and click "Parse".</div>';
    return;
  }

  let html = '';
  parsedBulkQuestions.forEach((q, idx) => {
    html += `
      <div class="card" style="margin-bottom: 12px; border-left: 4px solid var(--primary);" id="preview-q-${idx}">
        <div class="card-header" style="padding: 10px 16px;">
          <strong style="color: var(--navy-dark);">#${idx + 1}.</strong>
          <div style="display: flex; gap: 8px; align-items: center;">
            <span class="badge badge-info">Marks: ${q.marks}</span>
            <span class="badge badge-warning">Neg: -${q.negative_marks}</span>
            <span class="badge badge-success">Time: ${q.time_limit_seconds}s</span>
            <span class="badge" style="background:#f1f5f9; color:#0f172a;">Correct: <strong>${q.answer}</strong></span>
            <button type="button" class="btn btn-sm btn-danger" onclick="removeParsedQuestion(${idx})">Remove</button>
          </div>
        </div>
        <div class="card-body" style="padding: 12px 16px;">
          <p style="font-weight: 600; margin-bottom: 8px;">${q.question_text}</p>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; font-size: 0.88rem;">
            ${q.options.map(opt => `
              <div style="padding: 6px 10px; border-radius: 4px; background: ${opt.key === q.answer ? '#ecfdf5' : '#f8fafc'}; border: 1px solid ${opt.key === q.answer ? '#a7f3d0' : '#e2e8f0'};">
                <strong>${opt.key})</strong> ${opt.text} ${opt.key === q.answer ? '<span style="color:#059669; font-weight:700;">✓ Correct</span>' : ''}
              </div>
            `).join('')}
          </div>
        </div>
      </div>
    `;
  });

  container.innerHTML = html;
}

function removeParsedQuestion(idx) {
  parsedBulkQuestions.splice(idx, 1);
  renderParsedPreview();
  const confirmBtn = document.getElementById('btn-confirm-bulk-save');
  if (confirmBtn) confirmBtn.disabled = parsedBulkQuestions.length === 0;
}

function saveBulkQuestions() {
  if (parsedBulkQuestions.length === 0) {
    showToast('No questions to save.', 'danger');
    return;
  }

  const assessmentSelect = document.getElementById('bulk-target-assessment');
  const sectionSelect = document.getElementById('bulk-target-section');
  const assessmentId = assessmentSelect ? assessmentSelect.value : null;
  const sectionId = sectionSelect ? sectionSelect.value : null;

  const btn = document.getElementById('btn-confirm-bulk-save');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = 'Saving to Database...';
  }

  fetch('/admin/questions/bulk-save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      questions: parsedBulkQuestions,
      assessment_id: assessmentId,
      section_id: sectionId
    })
  })
    .then((res) => res.json())
    .then((data) => {
      if (data.success) {
        showToast(data.message, 'success');
        setTimeout(() => window.location.reload(), 1200);
      } else {
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = 'Confirm & Import to Bank';
        }
        showToast(data.message || 'Failed to save questions.', 'danger');
      }
    })
    .catch((err) => {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = 'Confirm & Import to Bank';
      }
      showToast('Error saving questions: ' + err, 'danger');
    });
}

// ==========================================================================
// CANDIDATE ASSIGNMENT LINKS MODAL
// ==========================================================================

function viewCandidateAssignments(candidateId) {
  fetch(`/admin/candidates/${candidateId}/assignments`)
    .then((res) => res.json())
    .then((data) => {
      const container = document.getElementById('candidate-links-content');
      const nameHeader = document.getElementById('candidate-links-name');
      if (nameHeader) nameHeader.innerText = `${data.candidate.name} (${data.candidate.candidate_id})`;

      if (!container) return;

      if (data.assignments.length === 0) {
        container.innerHTML = '<div style="padding: 20px; text-align: center; color: var(--text-muted);">No assessments currently assigned to this candidate.</div>';
      } else {
        let html = '';
        data.assignments.forEach((a) => {
          html += `
            <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px; margin-bottom: 12px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <strong style="color: var(--navy-dark);">${a.assessment_title}</strong>
                <span class="badge ${a.status === 'completed' ? 'badge-success' : 'badge-info'}">${a.status.toUpperCase()}</span>
              </div>
              <div style="display: flex; gap: 8px;">
                <input type="text" class="form-control" readonly value="${a.exam_url}" id="link-${a.assignment_id}" style="font-family: var(--font-mono); font-size: 0.82rem;">
                <button type="button" class="btn btn-primary btn-sm" onclick="copyToClipboard('${a.exam_url}')">Copy</button>
              </div>
            </div>
          `;
        });
        container.innerHTML = html;
      }

      openModal('modal-candidate-links');
    })
    .catch((err) => showToast('Error fetching candidate links: ' + err, 'danger'));
}

// ==========================================================================
// LIVE MONITORING REAL-TIME ENGINE
// ==========================================================================

let monitoringInterval = null;

function startLiveMonitoring() {
  const tableBody = document.getElementById('live-monitoring-tbody');
  if (!tableBody) return;

  function refreshLiveData() {
    fetch('/admin/api/monitoring/live')
      .then((res) => res.json())
      .then((data) => {
        // Update metric badges
        const activeCountEl = document.getElementById('live-active-count');
        const submissionsTodayEl = document.getElementById('live-submissions-today');
        const violationsTodayEl = document.getElementById('live-violations-today');

        if (activeCountEl) activeCountEl.innerText = data.active_count;
        if (submissionsTodayEl) submissionsTodayEl.innerText = data.submissions_today;
        if (violationsTodayEl) violationsTodayEl.innerText = data.total_violations_today;

        // Render Active Table
        if (data.active_attempts.length === 0) {
          tableBody.innerHTML = `
            <tr>
              <td colspan="8" style="text-align: center; padding: 36px; color: var(--text-muted);">
                <div style="font-size: 1.1rem; font-weight: 600; margin-bottom: 4px;">No active examinations right now</div>
                <div style="font-size: 0.85rem;">When candidates start their assessments, their real-time telemetry and anti-malpractice status will appear here.</div>
              </td>
            </tr>
          `;
          return;
        }

        let rows = '';
        data.active_attempts.forEach((att) => {
          const mins = Math.floor(att.remaining_seconds / 60);
          const secs = att.remaining_seconds % 60;
          const timeFormatted = `${mins}:${secs < 10 ? '0' : ''}${secs}`;

          const violationBadge = att.violations_count > 0
            ? `<span class="badge badge-danger">${att.violations_count} / ${att.max_violations}</span>`
            : `<span class="badge badge-success">0 Clean</span>`;

          const liveIndicator = att.is_live
            ? `<span style="display:inline-block; width:10px; height:10px; background:#10b981; border-radius:50%; box-shadow: 0 0 6px #10b981;" title="Connected"></span> <span style="font-size:0.8rem; color:#059669; font-weight:600;">Active</span>`
            : `<span style="display:inline-block; width:10px; height:10px; background:#f59e0b; border-radius:50%;" title="Heartbeat delayed"></span> <span style="font-size:0.8rem; color:#d97706;">Inactive</span>`;

          rows += `
            <tr>
              <td>
                <div style="font-weight: 600; color: var(--navy-dark);">${att.candidate_name}</div>
                <div style="font-size: 0.78rem; color: var(--text-muted);">${att.candidate_roll} • ${att.candidate_email}</div>
              </td>
              <td>${att.assessment_title}</td>
              <td>${liveIndicator}</td>
              <td style="font-family: var(--font-mono); font-weight: 700; color: ${att.remaining_seconds < 300 ? '#e11d48' : '#0f172a'};">${timeFormatted}</td>
              <td><span class="badge badge-info">Q ${att.current_question} / ${att.total_questions}</span></td>
              <td>${violationBadge}</td>
              <td style="font-size: 0.82rem; color: var(--text-muted);">${att.client_ip}</td>
              <td>
                <button type="button" class="btn btn-sm btn-danger" onclick="promptTerminateAttempt(${att.attempt_id}, '${att.candidate_name}')">
                  Terminate
                </button>
              </td>
            </tr>
          `;
        });
        tableBody.innerHTML = rows;
      })
      .catch((err) => console.error('Error fetching live monitoring data:', err));
  }

  refreshLiveData();
  monitoringInterval = setInterval(refreshLiveData, 3500);
}

function promptTerminateAttempt(attemptId, candidateName) {
  const reason = prompt(`Terminate exam session for ${candidateName}?\nPlease enter reason:`, 'Malpractice violation detected.');
  if (reason) {
    fetch(`/admin/api/monitoring/terminate/${attemptId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: reason })
    })
      .then((res) => res.json())
      .then((data) => {
        showToast(data.message, 'warning');
      })
      .catch((err) => showToast('Error terminating attempt: ' + err, 'danger'));
  }
}

// ==========================================================================
// RESULTS & AUDIT BREAKDOWN MODAL
// ==========================================================================

function viewAttemptDetails(attemptId) {
  fetch(`/admin/results/${attemptId}/details`)
    .then((res) => res.json())
    .then((data) => {
      const modal = document.getElementById('modal-attempt-details');
      const body = document.getElementById('attempt-details-body');
      if (!body) return;

      const att = data.attempt;
      let html = `
        <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px; margin-bottom: 20px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
            <div>
              <h3 style="margin: 0; color: var(--navy-dark);">${att.candidate_name}</h3>
              <div style="color: var(--text-muted); font-size: 0.85rem;">${att.candidate_roll} • ${att.candidate_email}</div>
            </div>
            <span class="badge ${att.passed ? 'badge-passed' : 'badge-failed'}" style="font-size: 0.95rem; padding: 6px 14px;">
              ${att.passed ? 'PASSED' : 'FAILED'} (${att.percentage}%)
            </span>
          </div>
          <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; font-size: 0.88rem;">
            <div><strong>Score:</strong> ${att.obtained_marks} / ${att.total_marks}</div>
            <div><strong>Correct:</strong> <span style="color:#059669; font-weight:700;">${att.correct_count}</span></div>
            <div><strong>Incorrect:</strong> <span style="color:#dc2626; font-weight:700;">${att.incorrect_count}</span></div>
            <div><strong>Violations:</strong> <span style="color:#ea580c; font-weight:700;">${att.violation_count}</span></div>
          </div>
          ${att.termination_reason ? `<div style="margin-top: 10px; color: #dc2626; font-weight: 600; font-size: 0.85rem;">Termination Note: ${att.termination_reason}</div>` : ''}
        </div>

        <h4 style="margin-bottom: 12px; color: var(--navy-dark);">Question Breakdown (${data.answers.length} items)</h4>
        <div style="display: flex; flex-direction: column; gap: 12px; margin-bottom: 24px;">
      `;

      data.answers.forEach((ans, idx) => {
        const isCorr = ans.is_correct === true;
        const isUnans = ans.selected_key === null;
        const statusBorder = isCorr ? '#10b981' : (isUnans ? '#cbd5e1' : '#ef4444');

        html += `
          <div style="background: #ffffff; border: 1px solid #e2e8f0; border-left: 4px solid ${statusBorder}; border-radius: 6px; padding: 14px;">
            <div style="display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 0.82rem;">
              <strong>Question ${idx + 1}</strong>
              <div>
                Marks Awarded: <strong style="color: ${isCorr ? '#059669' : (isUnans ? '#64748b' : '#dc2626')}">${ans.marks_awarded}</strong>
                ${ans.time_spent ? `<span style="color: var(--text-muted); margin-left: 8px;">(${ans.time_spent}s spent)</span>` : ''}
              </div>
            </div>
            <div style="font-weight: 600; margin-bottom: 8px; font-size: 0.95rem;">${ans.question_text}</div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; font-size: 0.85rem;">
              <div style="background: #f8fafc; padding: 8px 12px; border-radius: 4px;">
                <strong>Candidate Response:</strong> 
                ${ans.selected_key ? `<span style="color:${isCorr ? '#059669' : '#dc2626'}; font-weight:700;">Option ${ans.selected_key}) ${ans.selected_text}</span>` : '<span style="color:var(--text-muted); font-style:italic;">Unanswered</span>'}
              </div>
              <div style="background: #ecfdf5; padding: 8px 12px; border-radius: 4px; border: 1px solid #a7f3d0;">
                <strong>Correct Key:</strong> <span style="color:#059669; font-weight:700;">Option ${ans.correct_key}) ${ans.correct_text}</span>
              </div>
            </div>
          </div>
        `;
      });

      html += `</div>`;

      // Violations section
      if (data.violations && data.violations.length > 0) {
        html += `
          <h4 style="margin-bottom: 12px; color: #dc2626;">Anti-Malpractice Incident Log (${data.violations.length})</h4>
          <div class="table-responsive">
            <table class="table" style="font-size: 0.82rem;">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Incident Type</th>
                  <th>Description</th>
                </tr>
              </thead>
              <tbody>
                ${data.violations.map(v => `
                  <tr>
                    <td>${v.recorded_at ? v.recorded_at.replace('T', ' ').substring(0, 19) : ''}</td>
                    <td><span class="badge badge-danger">${v.violation_type}</span></td>
                    <td>${v.description}</td>
                  </tr>
                `).join('')}
              </tbody>
            </table>
          </div>
        `;
      }

      body.innerHTML = html;
      openModal('modal-attempt-details');
    })
    .catch((err) => showToast('Error loading candidate details: ' + err, 'danger'));
}

// Auto-initialize components on page load
document.addEventListener('DOMContentLoaded', () => {
  // Check if live monitoring table is present
  if (document.getElementById('live-monitoring-tbody')) {
    startLiveMonitoring();
  }

  // Bind CSV file input if present
  const csvInput = document.getElementById('bulk-csv-file');
  if (csvInput) {
    csvInput.addEventListener('change', handleCsvFileSelect);
  }
});
