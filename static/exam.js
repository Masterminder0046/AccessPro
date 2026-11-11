/**
 * ASSESSPRO — Candidate Examination & Anti-Malpractice Engine
 * TCS iON / Infosys Springboard Inspired Distraction-Free Cockpit
 */

class ExamEngine {
  constructor(token) {
    this.token = token;
    this.state = null;
    this.currentIndex = 0;
    this.totalRemainingSeconds = 0;
    this.questionRemainingSeconds = 0;
    this.questionTimerActive = false;
    this.timeSpentOnCurrent = 0;

    // Timers
    this.totalTimerInterval = null;
    this.questionTimerInterval = null;
    this.heartbeatInterval = null;
    this.timeSpentInterval = null;

    // Anti-Malpractice Flags
    this.lastViolationTime = 0;
    this.hasStarted = false;
    this.isSubmitting = false;

    this.init();
  }

  async init() {
    try {
      const res = await fetch(`/api/exam/${this.token}/state`);
      const data = await res.json();

      if (data.expired) {
        window.location.href = data.redirect || `/exam/${this.token}/completed`;
        return;
      }

      this.state = data;
      this.totalRemainingSeconds = data.remaining_seconds;
      this.currentIndex = 0;

      this.setupDOM();
      this.setupAntiMalpractice();
      this.startTotalTimer();
      this.startHeartbeat();
      this.loadQuestion(0);
      this.hasStarted = true;
    } catch (err) {
      console.error('Failed to initialize exam state:', err);
      alert('Network error connecting to assessment server. Please check your internet connection.');
    }
  }

  setupDOM() {
    // Populate Candidate & Assessment Meta
    const testTitleEl = document.getElementById('exam-assessment-title');
    const candNameEl = document.getElementById('exam-candidate-name');
    const candIdEl = document.getElementById('exam-candidate-id');
    const candAvatarEl = document.getElementById('exam-candidate-avatar');

    if (testTitleEl) testTitleEl.innerText = this.state.assessment.title;
    if (candNameEl) candNameEl.innerText = this.state.candidate.name;
    if (candIdEl) candIdEl.innerText = this.state.candidate.candidate_id;
    if (candAvatarEl) candAvatarEl.innerText = this.state.candidate.name.charAt(0).toUpperCase();

    // Render Question Palette Buttons
    this.renderPalette();
  }

  // ========================================================================
  // QUESTION DISPLAY & NAVIGATION
  // ========================================================================

  loadQuestion(index) {
    if (index < 0 || index >= this.state.questions.length) return;

    this.currentIndex = index;
    const q = this.state.questions[index];
    const saved = this.state.saved_answers[q.id] || {};

    // Header & Badges
    document.getElementById('q-number-label').innerText = `Question ${index + 1} of ${this.state.questions.length}`;
    document.getElementById('q-section-name').innerText = q.section || 'General';
    document.getElementById('q-marks-positive').innerText = `+${q.marks}`;
    document.getElementById('q-marks-negative').innerText = `-${q.negative_marks}`;

    // Question Statement
    document.getElementById('q-statement-text').innerText = q.text;

    // Render Options
    const optionsContainer = document.getElementById('options-list');
    optionsContainer.innerHTML = '';

    q.options.forEach((opt) => {
      const isSelected = saved.selected_option_id === opt.id;
      const card = document.createElement('div');
      card.className = `option-card ${isSelected ? 'selected' : ''}`;
      card.id = `opt-card-${opt.id}`;
      card.onclick = () => this.selectOption(opt.id);

      card.innerHTML = `
        <div class="opt-radio-circle"></div>
        <div class="opt-key-tag">${opt.key})</div>
        <div class="opt-text">${opt.text}</div>
      `;
      optionsContainer.appendChild(card);
    });

    // Navigation Buttons State
    const prevBtn = document.getElementById('btn-prev');
    if (prevBtn) {
      const allowBack = this.state.assessment.allow_backtracking;
      prevBtn.style.display = allowBack ? 'inline-flex' : 'none';
      prevBtn.disabled = index === 0;
    }

    // Individual Question Timer
    if (this.state.assessment.enable_question_timer) {
      this.startQuestionTimer(q.time_limit_seconds);
    } else {
      const qTimerBox = document.getElementById('question-timer-box');
      if (qTimerBox) qTimerBox.style.display = 'none';
    }

    this.updatePaletteActiveState();
    this.updatePaletteCounts();
  }

  selectOption(optionId) {
    const q = this.state.questions[this.currentIndex];
    if (!this.state.saved_answers[q.id]) {
      this.state.saved_answers[q.id] = { selected_option_id: null, is_marked_for_review: false, is_answered: false };
    }

    this.state.saved_answers[q.id].selected_option_id = optionId;
    this.state.saved_answers[q.id].is_answered = true;

    // Visual selection update
    document.querySelectorAll('.option-card').forEach((el) => el.classList.remove('selected'));
    const chosenCard = document.getElementById(`opt-card-${optionId}`);
    if (chosenCard) chosenCard.classList.add('selected');

    // Auto-save immediately
    this.saveCurrentAnswer(false);
    this.updatePaletteActiveState();
    this.updatePaletteCounts();
  }

  clearResponse() {
    const q = this.state.questions[this.currentIndex];
    if (this.state.saved_answers[q.id]) {
      this.state.saved_answers[q.id].selected_option_id = null;
      this.state.saved_answers[q.id].is_answered = false;
    }

    document.querySelectorAll('.option-card').forEach((el) => el.classList.remove('selected'));
    this.saveCurrentAnswer(false);
    this.updatePaletteActiveState();
    this.updatePaletteCounts();
  }

  saveAndNext() {
    this.saveCurrentAnswer(false);
    if (this.currentIndex < this.state.questions.length - 1) {
      this.loadQuestion(this.currentIndex + 1);
    } else {
      this.openSubmitModal();
    }
  }

  markForReviewAndNext() {
    const q = this.state.questions[this.currentIndex];
    if (!this.state.saved_answers[q.id]) {
      this.state.saved_answers[q.id] = { selected_option_id: null, is_marked_for_review: true, is_answered: false };
    } else {
      this.state.saved_answers[q.id].is_marked_for_review = true;
    }

    this.saveCurrentAnswer(true);
    if (this.currentIndex < this.state.questions.length - 1) {
      this.loadQuestion(this.currentIndex + 1);
    } else {
      this.openSubmitModal();
    }
  }

  navigatePrev() {
    if (this.currentIndex > 0 && this.state.assessment.allow_backtracking) {
      this.saveCurrentAnswer(false);
      this.loadQuestion(this.currentIndex - 1);
    }
  }

  async saveCurrentAnswer(isReview = false) {
    const q = this.state.questions[this.currentIndex];
    const saved = this.state.saved_answers[q.id] || {};

    const payload = {
      question_id: q.id,
      selected_option_id: saved.selected_option_id || null,
      is_marked_for_review: isReview || saved.is_marked_for_review || false,
      is_answered: Boolean(saved.selected_option_id),
      time_spent_seconds: this.timeSpentOnCurrent
    };

    this.timeSpentOnCurrent = 0; // Reset question time spent counter

    try {
      await fetch(`/api/exam/${this.token}/save-answer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    } catch (e) {
      console.warn('Silent auto-save retry queued:', e);
    }
  }

  // ========================================================================
  // QUESTION PALETTE (TCS COLOR SYSTEM)
  // ========================================================================

  renderPalette() {
    const container = document.getElementById('palette-btn-grid');
    if (!container) return;

    container.innerHTML = '';
    this.state.questions.forEach((q, idx) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'palette-btn not-visited';
      btn.id = `palette-btn-${idx}`;
      btn.innerText = idx + 1;
      btn.onclick = () => {
        this.saveCurrentAnswer(false);
        this.loadQuestion(idx);
      };
      container.appendChild(btn);
    });

    this.updatePaletteActiveState();
    this.updatePaletteCounts();
  }

  updatePaletteActiveState() {
    this.state.questions.forEach((q, idx) => {
      const btn = document.getElementById(`palette-btn-${idx}`);
      if (!btn) return;

      const saved = this.state.saved_answers[q.id];
      btn.className = 'palette-btn';

      if (idx === this.currentIndex) {
        btn.classList.add('current');
      }

      if (saved) {
        if (saved.is_marked_for_review && saved.is_answered) {
          btn.classList.add('answered-review');
        } else if (saved.is_marked_for_review) {
          btn.classList.add('review');
        } else if (saved.is_answered) {
          btn.classList.add('answered');
        } else {
          btn.classList.add('not-answered');
        }
      } else {
        btn.classList.add(idx === this.currentIndex ? 'not-answered' : 'not-visited');
      }
    });
  }

  updatePaletteCounts() {
    let answered = 0;
    let notAnswered = 0;
    let markedReview = 0;
    let notVisited = 0;

    this.state.questions.forEach((q, idx) => {
      const saved = this.state.saved_answers[q.id];
      if (saved) {
        if (saved.is_marked_for_review) {
          markedReview++;
        } else if (saved.is_answered) {
          answered++;
        } else {
          notAnswered++;
        }
      } else {
        if (idx === this.currentIndex) {
          notAnswered++;
        } else {
          notVisited++;
        }
      }
    });

    const elAns = document.getElementById('count-answered');
    const elNotAns = document.getElementById('count-not-answered');
    const elRev = document.getElementById('count-review');
    const elNotVis = document.getElementById('count-not-visited');

    if (elAns) elAns.innerText = answered;
    if (elNotAns) elNotAns.innerText = notAnswered;
    if (elRev) elRev.innerText = markedReview;
    if (elNotVis) elNotVis.innerText = notVisited;
  }

  // ========================================================================
  // TIMERS & COUNTDOWNS
  // ========================================================================

  startTotalTimer() {
    const timerBox = document.getElementById('total-timer-box');
    const timerText = document.getElementById('total-timer-text');

    const updateDisplay = () => {
      if (this.totalRemainingSeconds <= 0) {
        clearInterval(this.totalTimerInterval);
        this.autoSubmitTimeout();
        return;
      }

      const hrs = Math.floor(this.totalRemainingSeconds / 3600);
      const mins = Math.floor((this.totalRemainingSeconds % 3600) / 60);
      const secs = this.totalRemainingSeconds % 60;

      let formatted = '';
      if (hrs > 0) {
        formatted = `${hrs}:${mins < 10 ? '0' : ''}${mins}:${secs < 10 ? '0' : ''}${secs}`;
      } else {
        formatted = `${mins < 10 ? '0' : ''}${mins}:${secs < 10 ? '0' : ''}${secs}`;
      }

      if (timerText) timerText.innerText = formatted;

      // Visual warning when < 5 mins
      if (this.totalRemainingSeconds <= 300 && timerBox) {
        timerBox.classList.add('warning');
      }

      this.totalRemainingSeconds--;
      this.timeSpentOnCurrent++;
    };

    updateDisplay();
    this.totalTimerInterval = setInterval(updateDisplay, 1000);
  }

  startQuestionTimer(secondsLimit) {
    clearInterval(this.questionTimerInterval);
    const qTimerBox = document.getElementById('question-timer-box');
    const qTimerText = document.getElementById('question-timer-text');

    if (qTimerBox) qTimerBox.style.display = 'flex';
    this.questionRemainingSeconds = secondsLimit;

    const updateQDisplay = () => {
      if (this.questionRemainingSeconds <= 0) {
        clearInterval(this.questionTimerInterval);
        // Auto advance to next question
        this.saveCurrentAnswer(false);
        if (this.currentIndex < this.state.questions.length - 1) {
          this.loadQuestion(this.currentIndex + 1);
        } else {
          this.openSubmitModal();
        }
        return;
      }

      if (qTimerText) qTimerText.innerText = `${this.questionRemainingSeconds}s`;
      this.questionRemainingSeconds--;
    };

    updateQDisplay();
    this.questionTimerInterval = setInterval(updateQDisplay, 1000);
  }

  startHeartbeat() {
    this.heartbeatInterval = setInterval(async () => {
      if (this.isSubmitting) return;

      try {
        const res = await fetch(`/api/exam/${this.token}/heartbeat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ current_question_index: this.currentIndex })
        });
        const data = await res.json();
        if (data.expired) {
          window.location.href = data.redirect;
        } else if (typeof data.remaining_seconds === 'number') {
          // Adjust client timer slightly to avoid large clock drifts
          if (Math.abs(this.totalRemainingSeconds - data.remaining_seconds) > 5) {
            this.totalRemainingSeconds = data.remaining_seconds;
          }
        }
      } catch (e) {
        console.warn('Heartbeat tick missed:', e);
      }
    }, 12000);
  }

  // ========================================================================
  // ANTI-MALPRACTICE MONITORING
  // ========================================================================

  setupAntiMalpractice() {
    const malSettings = this.state.assessment;

    // Fullscreen enforcement
    if (malSettings.strict_fullscreen) {
      document.addEventListener('fullscreenchange', () => {
        if (!document.fullscreenElement && this.hasStarted && !this.isSubmitting) {
          this.triggerViolation('fullscreen_exit', 'Exited examination fullscreen mode.');
        }
      });
    }

    // Visibility change / Tab switch
    document.addEventListener('visibilitychange', () => {
      if (document.hidden && this.hasStarted && !this.isSubmitting) {
        this.triggerViolation('tab_switch', 'Candidate switched browser tab or minimized window.');
      }
    });

    // Window focus loss
    window.addEventListener('blur', () => {
      if (this.hasStarted && !this.isSubmitting) {
        this.triggerViolation('focus_lost', 'Browser window lost active focus.');
      }
    });

    // Block Copy / Cut / Paste
    if (malSettings.block_copy_paste) {
      ['copy', 'cut', 'paste'].forEach((evt) => {
        document.addEventListener(evt, (e) => {
          e.preventDefault();
          this.triggerViolation('clipboard_attempt', `Attempted to ${evt} content during examination.`);
        });
      });

      // Block Right Click
      document.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        this.triggerViolation('right_click', 'Right-click context menu is prohibited during assessment.');
      });
    }

    // Keyboard lock for DevTools / Shortcuts
    document.addEventListener('keydown', (e) => {
      // F12 or Ctrl+Shift+I or Ctrl+Shift+C
      if (e.key === 'F12' || (e.ctrlKey && e.shiftKey && (e.key === 'I' || e.key === 'J' || e.key === 'C'))) {
        e.preventDefault();
        this.triggerViolation('devtools_shortcut', 'Restricted developer tools shortcut key pressed.');
      }
      // Ctrl+U (View Source)
      if (e.ctrlKey && (e.key === 'u' || e.key === 'U')) {
        e.preventDefault();
        this.triggerViolation('restricted_key', 'View-source shortcut restricted.');
      }
    });
  }

  async triggerViolation(type, description) {
    const now = Date.now();
    // Debounce duplicate triggers within 2 seconds
    if (now - this.lastViolationTime < 2000) return;
    this.lastViolationTime = now;

    try {
      const res = await fetch(`/api/exam/${this.token}/violation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          violation_type: type,
          description: description,
          question_index: this.currentIndex + 1
        })
      });

      const data = await res.json();
      this.state.violation_count = data.violation_count;

      if (data.terminated) {
        this.isSubmitting = true;
        this.showTerminationNotice(data.violation_count, data.max_violations, data.redirect);
      } else {
        this.showViolationWarning(data.violation_count, data.max_violations, description);
      }
    } catch (e) {
      console.error('Failed to log violation:', e);
    }
  }

  showViolationWarning(count, maxAllowed, desc) {
    const banner = document.getElementById('violation-alert-box');
    const msg = document.getElementById('violation-alert-msg');
    const countBadge = document.getElementById('violation-alert-count');

    if (banner && msg) {
      countBadge.innerText = `Warning ${count} of ${maxAllowed}`;
      msg.innerText = `${desc} Please stay on this tab in fullscreen. Further violations will result in automatic exam termination.`;
      banner.style.display = 'flex';

      // Auto hide banner after 8s
      setTimeout(() => {
        banner.style.display = 'none';
      }, 8000);
    }
  }

  showTerminationNotice(count, maxAllowed, redirectUrl) {
    clearInterval(this.totalTimerInterval);
    clearInterval(this.questionTimerInterval);
    clearInterval(this.heartbeatInterval);

    const modal = document.getElementById('modal-termination');
    if (modal) {
      modal.classList.add('active');
      setTimeout(() => {
        window.location.href = redirectUrl || `/exam/${this.token}/completed`;
      }, 3500);
    } else {
      alert(`EXAMINATION TERMINATED: You exceeded the maximum allowed anti-malpractice violations (${count}/${maxAllowed}). Your responses have been submitted.`);
      window.location.href = redirectUrl || `/exam/${this.token}/completed`;
    }
  }

  // ========================================================================
  // SUBMISSION FLOW
  // ========================================================================

  openSubmitModal() {
    let answered = 0;
    let review = 0;
    let unans = 0;

    this.state.questions.forEach((q) => {
      const saved = this.state.saved_answers[q.id];
      if (saved && saved.is_answered) {
        answered++;
        if (saved.is_marked_for_review) review++;
      } else {
        unans++;
      }
    });

    document.getElementById('submit-modal-answered').innerText = answered;
    document.getElementById('submit-modal-review').innerText = review;
    document.getElementById('submit-modal-unanswered').innerText = unans;
    document.getElementById('submit-modal-total').innerText = this.state.questions.length;

    const modal = document.getElementById('modal-submit-confirm');
    if (modal) modal.classList.add('active');
  }

  closeSubmitModal() {
    const modal = document.getElementById('modal-submit-confirm');
    if (modal) modal.classList.remove('active');
  }

  async confirmSubmit() {
    if (this.isSubmitting) return;
    this.isSubmitting = true;

    clearInterval(this.totalTimerInterval);
    clearInterval(this.questionTimerInterval);
    clearInterval(this.heartbeatInterval);

    const btn = document.getElementById('btn-final-submit');
    if (btn) {
      btn.disabled = true;
      btn.innerText = 'Evaluating & Submitting...';
    }

    try {
      const res = await fetch(`/api/exam/${this.token}/submit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
      });
      const data = await res.json();
      window.location.href = data.redirect || `/exam/${this.token}/completed`;
    } catch (e) {
      alert('Error finalizing submission: ' + e);
      this.isSubmitting = false;
      if (btn) {
        btn.disabled = false;
        btn.innerText = 'Submit Examination';
      }
    }
  }

  autoSubmitTimeout() {
    if (this.isSubmitting) return;
    this.isSubmitting = true;

    alert('Your total examination duration has expired. Your responses are being automatically evaluated and recorded.');
    fetch(`/api/exam/${this.token}/submit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    }).finally(() => {
      window.location.href = `/exam/${this.token}/completed`;
    });
  }
}

// Global initialization helper
window.initExamEngine = function(token) {
  window.examEngine = new ExamEngine(token);
};
