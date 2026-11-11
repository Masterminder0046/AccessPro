import os
import csv
import io
import re
import json
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, jsonify, Response, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

from database import (
    db, init_app_database, get_utc_now, User, Candidate, Section, Question, Option,
    Assessment, AssessmentQuestion, Assignment, Attempt, Answer,
    Violation, ExamSession, ActivityLog, AdminLog
)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'assesspro-enterprise-secret-key-2026-xyz')
init_app_database(app)


# ---------------------------------------------------------
# AUTHENTICATION & ACCESS DECORATORS
# ---------------------------------------------------------

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'admin':
            flash('Please log in with administrator credentials.', 'warning')
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function


def log_admin_action(action, target_type=None, target_id=None, details=None):
    try:
        user_id = session.get('user_id')
        ip_addr = request.remote_addr
        log = AdminLog(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
            ip_address=ip_addr
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error logging admin action: {e}")


def log_candidate_activity(attempt_id, action, details=None):
    try:
        log = ActivityLog(
            attempt_id=attempt_id,
            action=action,
            details=details
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error logging candidate activity: {e}")


# ---------------------------------------------------------
# PARSERS & RANDOMIZATION ENGINE
# ---------------------------------------------------------

def generate_randomized_paper(assessment):
    """
    Generates an independently randomized question paper and option ordering on the server.
    - Section-wise randomization: groups questions by section, shuffles questions within sections.
    - Difficulty distribution: respects questions configured across difficulties.
    - Random option ordering: shuffles MCQ options independently for each question.
    - Consistent total marks and time limits across candidates.
    - Returns (question_ids_list, option_order_dict).
    """
    import random

    aqs = list(assessment.questions)
    if not aqs:
        return [], {}

    selected_aqs = []
    if assessment.randomize_sections:
        # Group by section id
        sec_map = {}
        for aq in aqs:
            s_id = aq.question.section_id if aq.question and aq.question.section_id else 0
            if s_id not in sec_map:
                sec_map[s_id] = []
            sec_map[s_id].append(aq)

        # Shuffle questions within each section
        for s_id in sorted(sec_map.keys()):
            group = list(sec_map[s_id])
            if assessment.randomize_questions:
                random.shuffle(group)
            selected_aqs.extend(group)
    else:
        selected_aqs = list(aqs)
        if assessment.randomize_questions:
            random.shuffle(selected_aqs)

    # Optional delivery subset
    if assessment.questions_to_deliver and 0 < assessment.questions_to_deliver < len(selected_aqs):
        selected_aqs = selected_aqs[:assessment.questions_to_deliver]

    q_ids = [aq.question_id for aq in selected_aqs]

    # Generate independent option order for each question
    option_order_map = {}
    for qid in q_ids:
        q = db.session.get(Question, qid)
        if q:
            opts = list(q.options)
            if assessment.randomize_options:
                random.shuffle(opts)
            option_order_map[str(qid)] = [opt.id for opt in opts]

    return q_ids, option_order_map


def parse_questions_from_text(raw_text):
    """
    Parses questions pasted into raw text area.
    Supports formats:
      1. What is Python?
      A) Option A
      B) Option B
      C) Option C
      D) Option D
      Answer: B
      Time: 30
      Marks: 2
      Negative: 0.5
    """
    if not raw_text or not raw_text.strip():
        return {'questions': [], 'errors': ['Textarea input is empty.'], 'warnings': []}

    text = raw_text.replace('\r\n', '\n').replace('\r', '\n')
    raw_blocks = re.split(r'\n\s*\n+', text.strip())

    questions = []
    errors = []
    warnings = []

    opt_pattern = re.compile(r'^(?:[A-Da-d][\.\)]|\([A-Da-d]\)|\[[A-Da-d]\])\s*(.+)$')
    ans_pattern = re.compile(r'^(?:Answer|Ans|Correct(?:\s*Answer)?)\s*[:=\-]\s*\(?([A-Da-d])\)?', re.IGNORECASE)
    time_pattern = re.compile(r'^(?:Time(?:\s*Limit)?)\s*[:=\-]\s*(\d+)', re.IGNORECASE)
    marks_pattern = re.compile(r'^(?:Marks?)\s*[:=\-]\s*([\d\.]+)', re.IGNORECASE)
    neg_pattern = re.compile(r'^(?:Negative(?:\s*Marks?)?|Neg)\s*[:=\-]\s*-?([\d\.]+)', re.IGNORECASE)
    exp_pattern = re.compile(r'^(?:Explanation|Exp)\s*[:=\-]\s*(.+)$', re.IGNORECASE)

    for idx, block in enumerate(raw_blocks, 1):
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        if not lines:
            continue

        q_lines = []
        options = []
        answer_key = None
        time_limit = 60
        marks = 1.0
        neg_marks = 0.0
        explanation = ''

        for line in lines:
            ans_m = ans_pattern.match(line)
            time_m = time_pattern.match(line)
            marks_m = marks_pattern.match(line)
            neg_m = neg_pattern.match(line)
            exp_m = exp_pattern.match(line)
            opt_m = opt_pattern.match(line)

            if ans_m:
                answer_key = ans_m.group(1).upper()
            elif time_m:
                time_limit = int(time_m.group(1))
            elif marks_m:
                marks = float(marks_m.group(1))
            elif neg_m:
                neg_marks = abs(float(neg_m.group(1)))
            elif exp_m:
                explanation = exp_m.group(1).strip()
            elif opt_m:
                first_char = line.lstrip('([ ')[:1].upper()
                opt_text = opt_m.group(1).strip()
                options.append({'key': first_char, 'text': opt_text})
            else:
                if not options:
                    q_lines.append(line)

        raw_q = ' '.join(q_lines).strip()
        clean_q = re.sub(r'^(?:Q\d+[\.\:\)]|\d+[\.\:\)])\s*', '', raw_q)
        block_identifier = f"Question #{idx} ('{clean_q[:30]}...' if clean_q else 'Empty')"

        if not clean_q:
            errors.append(f"{block_identifier}: Missing question statement text.")
            continue
        if len(options) < 2:
            errors.append(f"{block_identifier}: Must contain at least 2 options (found {len(options)}).")
            continue
        if not answer_key:
            errors.append(f"{block_identifier}: Missing correct Answer line (e.g. 'Answer: B').")
            continue

        valid_keys = [o['key'] for o in options]
        if answer_key not in valid_keys:
            errors.append(f"{block_identifier}: Correct answer '{answer_key}' does not match available options {valid_keys}.")
            continue

        questions.append({
            'question_text': clean_q,
            'options': options,
            'answer': answer_key,
            'time_limit_seconds': time_limit,
            'marks': marks,
            'negative_marks': neg_marks,
            'explanation': explanation
        })

    return {'questions': questions, 'errors': errors, 'warnings': warnings}


def parse_questions_from_csv(csv_content):
    """
    Parses questions from CSV file content.
    Expected header: question_text,option_a,option_b,option_c,option_d,answer,marks,negative_marks,time_limit,explanation
    """
    questions = []
    errors = []
    warnings = []

    try:
        reader = csv.DictReader(io.StringIO(csv_content))
        for row_idx, row in enumerate(reader, 2):
            q_text = row.get('question_text', '').strip()
            if not q_text:
                errors.append(f"Row {row_idx}: 'question_text' is missing.")
                continue

            options = []
            for key in ['A', 'B', 'C', 'D', 'E']:
                col_name = f'option_{key.lower()}'
                if col_name in row and row[col_name].strip():
                    options.append({'key': key, 'text': row[col_name].strip()})

            if len(options) < 2:
                errors.append(f"Row {row_idx}: Must provide at least 2 options.")
                continue

            answer = row.get('answer', '').strip().upper()
            if not answer:
                errors.append(f"Row {row_idx}: 'answer' key is missing.")
                continue

            valid_keys = [o['key'] for o in options]
            if answer not in valid_keys:
                errors.append(f"Row {row_idx}: Answer '{answer}' not in options {valid_keys}.")
                continue

            try:
                marks = float(row.get('marks', 1.0) or 1.0)
            except ValueError:
                marks = 1.0

            try:
                negative_marks = abs(float(row.get('negative_marks', 0.0) or 0.0))
            except ValueError:
                negative_marks = 0.0

            try:
                time_limit = int(row.get('time_limit', 60) or 60)
            except ValueError:
                time_limit = 60

            explanation = row.get('explanation', '').strip()

            questions.append({
                'question_text': q_text,
                'options': options,
                'answer': answer,
                'marks': marks,
                'negative_marks': negative_marks,
                'time_limit_seconds': time_limit,
                'explanation': explanation
            })
    except Exception as e:
        errors.append(f"CSV Parse error: {str(e)}")

    return {'questions': questions, 'errors': errors, 'warnings': warnings}


def evaluate_attempt(attempt):
    """
    Evaluates candidate attempt server-side.
    Calculates marks, correct/incorrect/unanswered, percentage, pass/fail status.
    Guarantees no client manipulation.
    """
    total_assessment_marks = 0.0
    obtained_marks = 0.0
    correct_count = 0
    incorrect_count = 0
    unanswered_count = 0

    aq_map = {aq.question_id: aq for aq in attempt.assessment.questions}

    for ans in attempt.answers:
        q = ans.question
        aq = aq_map.get(q.id)
        q_marks = aq.effective_marks if aq else q.marks
        q_neg_marks = aq.effective_negative_marks if aq else q.negative_marks

        total_assessment_marks += q_marks

        if ans.selected_option_id:
            selected_opt = db.session.get(Option, ans.selected_option_id)
            if selected_opt and selected_opt.is_correct:
                ans.is_correct = True
                ans.marks_awarded = q_marks
                correct_count += 1
                obtained_marks += q_marks
            else:
                ans.is_correct = False
                ans.marks_awarded = -q_neg_marks
                incorrect_count += 1
                obtained_marks -= q_neg_marks
        else:
            ans.is_correct = None
            ans.marks_awarded = 0.0
            unanswered_count += 1

    now = get_utc_now()
    time_taken = int((now - attempt.started_at).total_seconds()) if attempt.started_at else 0

    percentage = (obtained_marks / total_assessment_marks * 100) if total_assessment_marks > 0 else 0.0
    percentage = max(0.0, min(100.0, percentage))
    passed = percentage >= attempt.assessment.passing_percentage

    attempt.total_marks = total_assessment_marks
    attempt.obtained_marks = round(obtained_marks, 2)
    attempt.correct_count = correct_count
    attempt.incorrect_count = incorrect_count
    attempt.unanswered_count = unanswered_count
    attempt.percentage = round(percentage, 2)
    attempt.passed = passed
    attempt.time_taken_seconds = max(0, time_taken)
    attempt.submitted_at = now
    if attempt.status != 'terminated':
        attempt.status = 'submitted'

    if attempt.assignment:
        attempt.assignment.status = 'completed' if attempt.status == 'submitted' else 'terminated'

    # Close active sessions
    ExamSession.query.filter_by(attempt_id=attempt.id, is_active=True).update({'is_active': False})
    db.session.commit()
    return attempt


# ---------------------------------------------------------
# DATABASE SEEDING (20 QUESTIONS & 5 DEMO CANDIDATES)
# ---------------------------------------------------------

def seed_default_data():
    """Initializes tables and seeds 20 questions, 5 candidates, and default admin."""
    db.create_all()

    # Default Admin
    admin_user = User.query.filter_by(username='admin').first()
    if not admin_user:
        admin_user = User(
            username='admin',
            email='admin@assesspro.local',
            role='admin',
            full_name='System Administrator'
        )
        admin_user.set_password('admin123')
        db.session.add(admin_user)
        db.session.commit()
        app.logger.info("Created default administrator: admin / admin123")

    # Default Sections
    default_sections = ['Technical & Programming', 'General Aptitude', 'Verbal & Reasoning']
    sec_objs = {}
    for idx, s_name in enumerate(default_sections, 1):
        sec = Section.query.filter_by(name=s_name).first()
        if not sec:
            sec = Section(name=s_name, description=f'{s_name} assessment domain', order_index=idx)
            db.session.add(sec)
            db.session.flush()
        sec_objs[s_name] = sec
    db.session.commit()

    sec_tech = sec_objs['Technical & Programming']
    sec_apt = sec_objs['General Aptitude']
    sec_verb = sec_objs['Verbal & Reasoning']

    # Seed Sample Assessment with Common Link if none exists
    sample_assessment = Assessment.query.filter_by(code='GSE-2026-T1').first()
    if not sample_assessment:
        sample_assessment = Assessment(
            title='Graduate Software Engineer Assessment 2026',
            code='GSE-2026-T1',
            shared_slug='gse-2026',
            enable_shared_link=True,
            access_code=None,
            description='Corporate recruitment screening examination for software engineer roles, assessing core CS fundamentals, quantitative analysis, and reasoning skills.',
            instructions='1. Total duration is 45 minutes.\n2. Each question has individual timers and total countdown.\n3. Anti-malpractice active: switching tabs or exiting fullscreen triggers violation warnings.\n4. Negative marking applies (-0.5 per wrong answer).\n5. Each candidate receives an independently randomized paper.',
            duration_minutes=45,
            enable_question_timer=True,
            default_question_time=60,
            enable_negative_marking=True,
            negative_marking_rate=0.5,
            passing_percentage=60.0,
            randomize_questions=True,
            randomize_options=True,
            randomize_sections=True,
            max_attempts_per_candidate=1,
            allow_self_registration=True,
            allow_duplicate_attempts=False,
            allow_backtracking=True,
            max_violations=3,
            strict_fullscreen=True,
            block_copy_paste=True,
            status='published',
            created_by_id=admin_user.id
        )
        db.session.add(sample_assessment)
        db.session.commit()

        # 20 Comprehensive Questions
        twenty_questions = [
            # Technical & Programming (8 questions)
            {
                'text': 'Which of the following data structures operates on a First-In-First-Out (FIFO) principle?',
                'options': [('A', 'Stack', False), ('B', 'Queue', True), ('C', 'Tree', False), ('D', 'Graph', False)],
                'sec': sec_tech, 'marks': 2.0, 'neg': 0.5, 'time': 45, 'diff': 'easy', 'cat': 'Data Structures'
            },
            {
                'text': 'What is the time complexity of searching for an element in a balanced Binary Search Tree (AVL / Red-Black)?',
                'options': [('A', 'O(1)', False), ('B', 'O(n)', False), ('C', 'O(log n)', True), ('D', 'O(n log n)', False)],
                'sec': sec_tech, 'marks': 2.0, 'neg': 0.5, 'time': 45, 'diff': 'medium', 'cat': 'Algorithms'
            },
            {
                'text': 'Which HTTP status code signifies that a requested resource was successfully created on the server?',
                'options': [('A', '200 OK', False), ('B', '201 Created', True), ('C', '204 No Content', False), ('D', '301 Moved Permanently', False)],
                'sec': sec_tech, 'marks': 1.0, 'neg': 0.25, 'time': 30, 'diff': 'easy', 'cat': 'Web Technologies'
            },
            {
                'text': 'Which of the following built-in Python data types is immutable?',
                'options': [('A', 'list', False), ('B', 'dict', False), ('C', 'set', False), ('D', 'tuple', True)],
                'sec': sec_tech, 'marks': 1.0, 'neg': 0.25, 'time': 30, 'diff': 'easy', 'cat': 'Python'
            },
            {
                'text': 'Which database system is a relational database management system (RDBMS) providing full ACID guarantees?',
                'options': [('A', 'MongoDB', False), ('B', 'PostgreSQL', True), ('C', 'Redis', False), ('D', 'Apache Cassandra', False)],
                'sec': sec_tech, 'marks': 1.0, 'neg': 0.25, 'time': 30, 'diff': 'easy', 'cat': 'Databases'
            },
            {
                'text': 'Which algorithm does Python 3 utilize to resolve method lookup in multiple inheritance (Method Resolution Order)?',
                'options': [('A', 'Depth-First Search (DFS)', False), ('B', 'Breadth-First Search (BFS)', False), ('C', 'C3 Linearization', True), ('D', 'Dijkstra Algorithm', False)],
                'sec': sec_tech, 'marks': 3.0, 'neg': 1.0, 'time': 60, 'diff': 'hard', 'cat': 'Python'
            },
            {
                'text': 'In modern operating systems, which memory management mechanism divides virtual address space into fixed-size blocks?',
                'options': [('A', 'Paging', True), ('B', 'Dynamic Partitioning', False), ('C', 'Overlaying', False), ('D', 'Compaction', False)],
                'sec': sec_tech, 'marks': 2.0, 'neg': 0.5, 'time': 45, 'diff': 'medium', 'cat': 'Operating Systems'
            },
            {
                'text': 'Which transport layer protocol provides flow control, error recovery, and ordered delivery of data packets?',
                'options': [('A', 'UDP', False), ('B', 'TCP', True), ('C', 'ICMP', False), ('D', 'IP', False)],
                'sec': sec_tech, 'marks': 2.0, 'neg': 0.5, 'time': 45, 'diff': 'medium', 'cat': 'Computer Networks'
            },

            # General Aptitude (7 questions)
            {
                'text': 'If a train traveling at 72 km/h crosses a 180-meter long platform in 18 seconds, what is the length of the train?',
                'options': [('A', '180 meters', True), ('B', '150 meters', False), ('C', '200 meters', False), ('D', '220 meters', False)],
                'sec': sec_apt, 'marks': 2.0, 'neg': 0.5, 'time': 60, 'diff': 'medium', 'cat': 'Quantitative Aptitude'
            },
            {
                'text': 'A can complete a project in 10 days and B can complete it in 15 days. How many days will they take working together?',
                'options': [('A', '5 days', False), ('B', '6 days', True), ('C', '8 days', False), ('D', '12 days', False)],
                'sec': sec_apt, 'marks': 2.0, 'neg': 0.5, 'time': 60, 'diff': 'medium', 'cat': 'Quantitative Aptitude'
            },
            {
                'text': 'The price of an item increases by 25%. By what percentage must consumption be reduced so expenditure remains unchanged?',
                'options': [('A', '15%', False), ('B', '20%', True), ('C', '25%', False), ('D', '18.5%', False)],
                'sec': sec_apt, 'marks': 1.5, 'neg': 0.5, 'time': 45, 'diff': 'easy', 'cat': 'Quantitative Aptitude'
            },
            {
                'text': 'A retailer marks goods 33.33% above cost price and allows a 10% discount on the marked price. What is the net profit percentage?',
                'options': [('A', '20%', True), ('B', '23.33%', False), ('C', '15%', False), ('D', '25%', False)],
                'sec': sec_apt, 'marks': 2.5, 'neg': 0.75, 'time': 60, 'diff': 'hard', 'cat': 'Quantitative Aptitude'
            },
            {
                'text': 'A mixture contains milk and water in the ratio 3:1. If 5 liters of water are added, the ratio becomes 3:2. How much milk was originally in the mixture?',
                'options': [('A', '12 liters', False), ('B', '15 liters', True), ('C', '18 liters', False), ('D', '20 liters', False)],
                'sec': sec_apt, 'marks': 2.0, 'neg': 0.5, 'time': 60, 'diff': 'medium', 'cat': 'Quantitative Aptitude'
            },
            {
                'text': 'Find the next number in the logical sequence: 2, 6, 12, 20, 30, ?',
                'options': [('A', '36', False), ('B', '40', False), ('C', '42', True), ('D', '48', False)],
                'sec': sec_apt, 'marks': 1.0, 'neg': 0.25, 'time': 30, 'diff': 'easy', 'cat': 'Logical Reasoning'
            },
            {
                'text': 'When rolling two fair standard six-sided dice simultaneously, what is the probability that the sum of the faces equals 7?',
                'options': [('A', '1/12', False), ('B', '1/6', True), ('C', '5/36', False), ('D', '7/36', False)],
                'sec': sec_apt, 'marks': 2.0, 'neg': 0.5, 'time': 45, 'diff': 'medium', 'cat': 'Probability'
            },

            # Verbal & Reasoning (5 questions)
            {
                'text': 'Statements: (1) All engineers are innovators. (2) Some innovators are entrepreneurs. Which conclusion definitely follows?',
                'options': [('A', 'All engineers are entrepreneurs', False), ('B', 'Some innovators are engineers', True), ('C', 'No engineer is an entrepreneur', False), ('D', 'All entrepreneurs are innovators', False)],
                'sec': sec_verb, 'marks': 2.0, 'neg': 0.5, 'time': 45, 'diff': 'medium', 'cat': 'Logical Reasoning'
            },
            {
                'text': 'Select the grammatically correct sentence adhering to standard formal business English:',
                'options': [('A', 'The committee has submitted their report.', False), ('B', 'The committee have submitted its report.', False), ('C', 'The committee has submitted its report.', True), ('D', 'The committee are submitting its report.', False)],
                'sec': sec_verb, 'marks': 1.0, 'neg': 0.25, 'time': 30, 'diff': 'easy', 'cat': 'Verbal Ability'
            },
            {
                'text': '"While automation enhances productivity across routine tasks, strategic decision-making requires context-sensitive human judgment." What can be logically inferred?',
                'options': [('A', 'Automation will entirely replace executive leadership.', False), ('B', 'Automation is ineffective in modern enterprise.', False), ('C', 'Automation and human judgment complement each other.', True), ('D', 'Strategic decision-making cannot utilize data.', False)],
                'sec': sec_verb, 'marks': 2.5, 'neg': 0.75, 'time': 60, 'diff': 'hard', 'cat': 'Verbal Ability'
            },
            {
                'text': 'Complete the semantic analogy: Clock is to Time as Thermometer is to:',
                'options': [('A', 'Mercury', False), ('B', 'Temperature', True), ('C', 'Fahrenheit', False), ('D', 'Weather', False)],
                'sec': sec_verb, 'marks': 1.0, 'neg': 0.25, 'time': 30, 'diff': 'easy', 'cat': 'Verbal Ability'
            },
            {
                'text': 'Pointing to a gentleman, a woman said, "His mother is the only daughter of my mother." How is the woman related to the gentleman?',
                'options': [('A', 'Sister', False), ('B', 'Mother', True), ('C', 'Aunt', False), ('D', 'Grandmother', False)],
                'sec': sec_verb, 'marks': 2.0, 'neg': 0.5, 'time': 45, 'diff': 'medium', 'cat': 'Logical Reasoning'
            }
        ]

        for idx, q_info in enumerate(twenty_questions, 1):
            q = Question(
                section_id=q_info['sec'].id,
                question_text=q_info['text'],
                marks=q_info['marks'],
                negative_marks=q_info['neg'],
                time_limit_seconds=q_info['time'],
                difficulty=q_info['diff'],
                category=q_info['cat']
            )
            db.session.add(q)
            db.session.flush()

            for opt_key, opt_text, is_corr in q_info['options']:
                opt = Option(question_id=q.id, option_key=opt_key, option_text=opt_text, is_correct=is_corr)
                db.session.add(opt)

            aq = AssessmentQuestion(
                assessment_id=sample_assessment.id,
                question_id=q.id,
                order_index=idx,
                custom_marks=q_info['marks'],
                custom_negative_marks=q_info['neg'],
                custom_time_limit=q_info['time']
            )
            db.session.add(aq)

        # Seed 5 Demo Candidates
        demo_candidates = [
            {'cid': 'CAND-2026-001', 'name': 'Rohit Sharma', 'email': 'rohit.sharma@example.com', 'dept': 'Computer Science & Engineering', 'col': 'National Institute of Technology'},
            {'cid': 'CAND-2026-002', 'name': 'Priya Patel', 'email': 'priya.patel@example.com', 'dept': 'Information Technology', 'col': 'Indian Institute of Information Technology'},
            {'cid': 'CAND-2026-003', 'name': 'Ananya Sen', 'email': 'ananya.sen@example.com', 'dept': 'Electronics & Communication', 'col': 'Delhi Technological University'},
            {'cid': 'CAND-2026-004', 'name': 'Vikram Malhotra', 'email': 'vikram.m@example.com', 'dept': 'Mechanical & Automation', 'col': 'Birla Institute of Technology'},
            {'cid': 'CAND-2026-005', 'name': 'Sneha Reddy', 'email': 'sneha.reddy@example.com', 'dept': 'Data Science & AI', 'col': 'Vellore Institute of Technology'}
        ]

        for c_data in demo_candidates:
            if not Candidate.query.filter_by(candidate_id=c_data['cid']).first():
                cand = Candidate(
                    candidate_id=c_data['cid'],
                    name=c_data['name'],
                    email=c_data['email'],
                    phone='+91 98765 ' + c_data['cid'][-4:],
                    department=c_data['dept'],
                    college=c_data['col']
                )
                db.session.add(cand)

        db.session.commit()
        app.logger.info("Successfully seeded 20 questions, 5 demo candidates, and common assessment /exam/gse-2026")


# ---------------------------------------------------------
# AUTHENTICATION & CORE ROUTES
# ---------------------------------------------------------

@app.route('/')
def index():
    if 'user_id' in session and session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        user = User.query.filter((User.username == username) | (User.email == username)).first()
        if user and user.check_password(password):
            session.clear()
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            session['full_name'] = user.full_name or user.username
            flash(f'Welcome back, {user.full_name or user.username}!', 'success')
            log_admin_action('admin_login', 'user', user.id, 'Logged in to administrative portal')
            next_url = request.args.get('next')
            return redirect(next_url or url_for('admin_dashboard'))
        else:
            flash('Invalid administrator credentials. Please check your username and password.', 'danger')

    return render_template('login.html')


@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    if user_id:
        log_admin_action('admin_logout', 'user', user_id, 'Logged out from portal')
    session.clear()
    flash('You have been successfully logged out.', 'info')
    return redirect(url_for('login'))


# ---------------------------------------------------------
# ADMIN PORTAL ROUTES
# ---------------------------------------------------------

@app.route('/admin')
@app.route('/admin/dashboard')
@admin_required
def admin_dashboard():
    total_assessments = Assessment.query.count()
    active_exams = Assessment.query.filter_by(status='published').count()
    total_candidates = Candidate.query.count()
    total_questions = Question.query.count()

    # Attempts analytics
    all_attempts = Attempt.query.all()
    total_attempts = len(all_attempts)
    completed_attempts = [a for a in all_attempts if a.status in ('submitted', 'auto_submitted')]
    passed_attempts = [a for a in completed_attempts if a.passed]

    avg_score = round(sum(a.percentage for a in completed_attempts) / len(completed_attempts), 1) if completed_attempts else 0.0
    pass_rate = round((len(passed_attempts) / len(completed_attempts) * 100), 1) if completed_attempts else 0.0

    # Live active sessions
    five_mins_ago = get_utc_now() - timedelta(minutes=5)
    live_sessions_count = ExamSession.query.filter(
        ExamSession.is_active == True,
        ExamSession.last_heartbeat >= five_mins_ago
    ).count()

    recent_attempts = Attempt.query.order_by(Attempt.started_at.desc()).limit(8).all()
    recent_logs = AdminLog.query.order_by(AdminLog.created_at.desc()).limit(6).all()
    assessments_list = Assessment.query.order_by(Assessment.created_at.desc()).limit(5).all()

    return render_template(
        'admin.html',
        total_assessments=total_assessments,
        active_exams=active_exams,
        total_candidates=total_candidates,
        total_questions=total_questions,
        total_attempts=total_attempts,
        completed_attempts=len(completed_attempts),
        avg_score=avg_score,
        pass_rate=pass_rate,
        live_sessions_count=live_sessions_count,
        recent_attempts=recent_attempts,
        recent_logs=recent_logs,
        assessments_list=assessments_list
    )


# --- Assessments Management ---

@app.route('/admin/assessments')
@admin_required
def admin_assessments():
    assessments = Assessment.query.order_by(Assessment.created_at.desc()).all()
    candidates = Candidate.query.order_by(Candidate.name.asc()).all()
    sections = Section.query.all()
    return render_template(
        'assessments.html',
        assessments=assessments,
        candidates=candidates,
        sections=sections
    )


@app.route('/admin/assessments/create', methods=['POST'])
@admin_required
def create_assessment():
    title = request.form.get('title', '').strip()
    code = request.form.get('code', '').strip().upper()
    slug_input = request.form.get('shared_slug', '').strip().lower()
    description = request.form.get('description', '').strip()
    instructions = request.form.get('instructions', '').strip()

    if not title or not code:
        flash('Assessment Title and Unique Code are required.', 'danger')
        return redirect(url_for('admin_assessments'))

    existing = Assessment.query.filter((Assessment.code == code) | (Assessment.shared_slug == slug_input and slug_input != '')).first()
    if existing:
        flash(f'An assessment with code "{code}" or slug "{slug_input}" already exists.', 'warning')
        return redirect(url_for('admin_assessments'))

    # Slug fallback
    if not slug_input:
        slug_input = re.sub(r'[^a-z0-9\-]+', '-', code.lower()).strip('-')

    duration = int(request.form.get('duration_minutes', 60))
    enable_q_timer = 'enable_question_timer' in request.form
    default_q_time = int(request.form.get('default_question_time', 60))
    enable_neg = 'enable_negative_marking' in request.form
    neg_rate = float(request.form.get('negative_marking_rate', 0.25))
    passing_pct = float(request.form.get('passing_percentage', 50.0))
    rand_q = 'randomize_questions' in request.form
    rand_opt = 'randomize_options' in request.form
    rand_sec = 'randomize_sections' in request.form
    backtracking = 'allow_backtracking' in request.form
    max_violations = int(request.form.get('max_violations', 3))
    strict_fullscreen = 'strict_fullscreen' in request.form
    block_copy_paste = 'block_copy_paste' in request.form
    enable_shared = 'enable_shared_link' in request.form
    allow_self_reg = 'allow_self_registration' in request.form
    allow_duplicate = 'allow_duplicate_attempts' in request.form
    max_attempts = int(request.form.get('max_attempts_per_candidate', 1))
    access_code = request.form.get('access_code', '').strip() or None

    new_assessment = Assessment(
        title=title,
        code=code,
        shared_slug=slug_input,
        enable_shared_link=enable_shared,
        access_code=access_code,
        description=description,
        instructions=instructions,
        duration_minutes=duration,
        enable_question_timer=enable_q_timer,
        default_question_time=default_q_time,
        enable_negative_marking=enable_neg,
        negative_marking_rate=neg_rate,
        passing_percentage=passing_pct,
        randomize_questions=rand_q,
        randomize_options=rand_opt,
        randomize_sections=rand_sec,
        max_attempts_per_candidate=max_attempts,
        allow_self_registration=allow_self_reg,
        allow_duplicate_attempts=allow_duplicate,
        allow_backtracking=backtracking,
        max_violations=max_violations,
        strict_fullscreen=strict_fullscreen,
        block_copy_paste=block_copy_paste,
        status='draft',
        created_by_id=session.get('user_id')
    )
    db.session.add(new_assessment)
    db.session.commit()

    log_admin_action('create_assessment', 'assessment', new_assessment.id, f'Created assessment: {title} ({code}) with common link /exam/{slug_input}')
    flash(f'Assessment "{title}" created! Common link: /exam/{slug_input}', 'success')
    return redirect(url_for('admin_assessments'))


@app.route('/admin/assessments/<int:id>/edit', methods=['POST'])
@admin_required
def edit_assessment(id):
    assessment = db.session.get(Assessment, id)
    if not assessment:
        abort(404)

    assessment.title = request.form.get('title', assessment.title).strip()
    slug_val = request.form.get('shared_slug', '').strip().lower()
    if slug_val:
        # Check uniqueness
        other = Assessment.query.filter(Assessment.shared_slug == slug_val, Assessment.id != id).first()
        if other:
            flash(f'Shared slug "{slug_val}" is already taken by another assessment.', 'warning')
        else:
            assessment.shared_slug = slug_val

    assessment.description = request.form.get('description', '').strip()
    assessment.instructions = request.form.get('instructions', '').strip()
    assessment.duration_minutes = int(request.form.get('duration_minutes', assessment.duration_minutes))
    assessment.enable_question_timer = 'enable_question_timer' in request.form
    assessment.default_question_time = int(request.form.get('default_question_time', assessment.default_question_time))
    assessment.enable_negative_marking = 'enable_negative_marking' in request.form
    assessment.negative_marking_rate = float(request.form.get('negative_marking_rate', assessment.negative_marking_rate))
    assessment.passing_percentage = float(request.form.get('passing_percentage', assessment.passing_percentage))
    assessment.randomize_questions = 'randomize_questions' in request.form
    assessment.randomize_options = 'randomize_options' in request.form
    assessment.randomize_sections = 'randomize_sections' in request.form
    assessment.enable_shared_link = 'enable_shared_link' in request.form
    assessment.allow_self_registration = 'allow_self_registration' in request.form
    assessment.allow_duplicate_attempts = 'allow_duplicate_attempts' in request.form
    assessment.max_attempts_per_candidate = int(request.form.get('max_attempts_per_candidate', assessment.max_attempts_per_candidate or 1))
    assessment.access_code = request.form.get('access_code', '').strip() or None
    assessment.allow_backtracking = 'allow_backtracking' in request.form
    assessment.max_violations = int(request.form.get('max_violations', assessment.max_violations))
    assessment.strict_fullscreen = 'strict_fullscreen' in request.form
    assessment.block_copy_paste = 'block_copy_paste' in request.form

    db.session.commit()
    log_admin_action('edit_assessment', 'assessment', assessment.id, f'Updated assessment details: {assessment.title}')
    flash('Assessment settings updated successfully.', 'success')
    return redirect(url_for('admin_assessments'))


@app.route('/admin/assessments/<int:id>/toggle-shared-link', methods=['POST'])
@admin_required
def toggle_shared_link(id):
    assessment = db.session.get(Assessment, id)
    if not assessment:
        abort(404)
    assessment.enable_shared_link = not assessment.enable_shared_link
    db.session.commit()
    st = "enabled" if assessment.enable_shared_link else "disabled"
    log_admin_action('toggle_shared_link', 'assessment', id, f'Shared link {st}')
    flash(f'Common examination link has been {st}.', 'info')
    return redirect(url_for('admin_assessments'))


@app.route('/admin/assessments/<int:id>/publish', methods=['POST'])
@admin_required
def publish_assessment(id):
    assessment = db.session.get(Assessment, id)
    if not assessment:
        abort(404)
    new_status = 'published' if assessment.status != 'published' else 'draft'
    assessment.status = new_status
    db.session.commit()
    log_admin_action('toggle_publish', 'assessment', assessment.id, f'Changed status to {new_status}')
    flash(f'Assessment status updated to {new_status.upper()}.', 'info')
    return redirect(url_for('admin_assessments'))


@app.route('/admin/assessments/<int:id>/delete', methods=['POST'])
@admin_required
def delete_assessment(id):
    assessment = db.session.get(Assessment, id)
    if not assessment:
        abort(404)
    title = assessment.title
    db.session.delete(assessment)
    db.session.commit()
    log_admin_action('delete_assessment', 'assessment', id, f'Deleted assessment: {title}')
    flash(f'Assessment "{title}" deleted successfully.', 'success')
    return redirect(url_for('admin_assessments'))


@app.route('/admin/assessments/<int:id>/assign', methods=['POST'])
@admin_required
def assign_assessment(id):
    assessment = db.session.get(Assessment, id)
    if not assessment:
        abort(404)
    candidate_ids = request.form.getlist('candidate_ids')
    assigned_count = 0

    for c_id in candidate_ids:
        c_id = int(c_id)
        existing = Assignment.query.filter_by(assessment_id=id, candidate_id=c_id).first()
        if not existing:
            token = secrets.token_urlsafe(16)
            assignment = Assignment(
                assessment_id=id,
                candidate_id=c_id,
                access_token=token,
                status='assigned'
            )
            db.session.add(assignment)
            assigned_count += 1

    db.session.commit()
    log_admin_action('assign_candidates', 'assessment', id, f'Assigned {assigned_count} candidates')
    flash(f'Successfully assigned {assigned_count} candidate(s) to "{assessment.title}".', 'success')
    return redirect(url_for('admin_assessments'))


@app.route('/admin/assessments/<int:id>/questions', methods=['GET', 'POST'])
@admin_required
def manage_assessment_questions(id):
    assessment = db.session.get(Assessment, id)
    if not assessment:
        abort(404)

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'attach_questions':
            selected_q_ids = request.form.getlist('question_ids')
            current_count = len(assessment.questions)
            for q_id in selected_q_ids:
                q_id = int(q_id)
                already = AssessmentQuestion.query.filter_by(assessment_id=id, question_id=q_id).first()
                if not already:
                    current_count += 1
                    q = db.session.get(Question, q_id)
                    aq = AssessmentQuestion(
                        assessment_id=id,
                        question_id=q_id,
                        order_index=current_count,
                        custom_marks=q.marks if q else 1.0,
                        custom_negative_marks=q.negative_marks if q else 0.0,
                        custom_time_limit=q.time_limit_seconds if q else 60
                    )
                    db.session.add(aq)
            db.session.commit()
            flash('Questions attached to assessment successfully.', 'success')
        elif action == 'remove_question':
            aq_id = int(request.form.get('aq_id'))
            aq = db.session.get(AssessmentQuestion, aq_id)
            if aq and aq.assessment_id == id:
                db.session.delete(aq)
                db.session.commit()
                flash('Question removed from assessment.', 'info')
        return redirect(url_for('manage_assessment_questions', id=id))

    attached_q_ids = [aq.question_id for aq in assessment.questions]
    available_questions = Question.query.filter(~Question.id.in_(attached_q_ids if attached_q_ids else [-1])).all()
    sections = Section.query.all()

    return render_template(
        'questions.html',
        mode='manage_assessment',
        assessment=assessment,
        available_questions=available_questions,
        sections=sections
    )


# --- Question Bank & Bulk Parsing ---

@app.route('/admin/questions')
@admin_required
def admin_questions():
    questions = Question.query.order_by(Question.created_at.desc()).all()
    sections = Section.query.all()
    assessments = Assessment.query.order_by(Assessment.created_at.desc()).all()
    return render_template(
        'questions.html',
        mode='bank',
        questions=questions,
        sections=sections,
        assessments=assessments
    )


@app.route('/admin/questions/create', methods=['POST'])
@admin_required
def create_question():
    q_text = request.form.get('question_text', '').strip()
    sec_id = request.form.get('section_id')
    sec_id = int(sec_id) if sec_id else None
    marks = float(request.form.get('marks', 1.0))
    negative_marks = abs(float(request.form.get('negative_marks', 0.0)))
    time_limit = int(request.form.get('time_limit_seconds', 60))
    difficulty = request.form.get('difficulty', 'medium')
    category = request.form.get('category', 'General')
    explanation = request.form.get('explanation', '').strip()
    correct_opt_key = request.form.get('correct_option', 'A').upper()

    if not q_text:
        flash('Question statement is required.', 'danger')
        return redirect(url_for('admin_questions'))

    new_q = Question(
        section_id=sec_id,
        question_text=q_text,
        marks=marks,
        negative_marks=negative_marks,
        time_limit_seconds=time_limit,
        difficulty=difficulty,
        category=category,
        explanation=explanation
    )
    db.session.add(new_q)
    db.session.flush()

    for key in ['A', 'B', 'C', 'D']:
        opt_text = request.form.get(f'option_{key.lower()}', '').strip()
        if opt_text:
            is_corr = (key == correct_opt_key)
            opt = Option(question_id=new_q.id, option_key=key, option_text=opt_text, is_correct=is_corr)
            db.session.add(opt)

    db.session.commit()
    log_admin_action('create_question', 'question', new_q.id, f'Created question in category {category}')
    flash('Question added to bank successfully.', 'success')
    return redirect(url_for('admin_questions'))


@app.route('/admin/questions/<int:id>/edit', methods=['POST'])
@admin_required
def edit_question(id):
    question = db.session.get(Question, id)
    if not question:
        abort(404)
    question.question_text = request.form.get('question_text', question.question_text).strip()
    sec_id = request.form.get('section_id')
    question.section_id = int(sec_id) if sec_id else None
    question.marks = float(request.form.get('marks', question.marks))
    question.negative_marks = abs(float(request.form.get('negative_marks', question.negative_marks)))
    question.time_limit_seconds = int(request.form.get('time_limit_seconds', question.time_limit_seconds))
    question.difficulty = request.form.get('difficulty', question.difficulty)
    question.explanation = request.form.get('explanation', '').strip()

    correct_key = request.form.get('correct_option', 'A').upper()
    for opt in question.options:
        new_text = request.form.get(f'option_{opt.option_key.lower()}')
        if new_text is not None:
            opt.option_text = new_text.strip()
        opt.is_correct = (opt.option_key == correct_key)

    db.session.commit()
    log_admin_action('edit_question', 'question', question.id, 'Updated question details')
    flash('Question updated successfully.', 'success')
    return redirect(url_for('admin_questions'))


@app.route('/admin/questions/<int:id>/delete', methods=['POST'])
@admin_required
def delete_question(id):
    question = db.session.get(Question, id)
    if not question:
        abort(404)
    db.session.delete(question)
    db.session.commit()
    log_admin_action('delete_question', 'question', id, 'Deleted question')
    flash('Question deleted successfully.', 'success')
    return redirect(url_for('admin_questions'))


@app.route('/admin/questions/bulk-parse', methods=['POST'])
@admin_required
def bulk_parse_questions():
    """API endpoint to parse text or CSV questions and return editable preview JSON."""
    data = request.get_json(silent=True) or {}
    raw_text = data.get('raw_text', '')

    if 'csv_file' in request.files:
        file = request.files['csv_file']
        if file and file.filename.endswith('.csv'):
            content = file.read().decode('utf-8', errors='ignore')
            result = parse_questions_from_csv(content)
            return jsonify(result)

    result = parse_questions_from_text(raw_text)
    return jsonify(result)


@app.route('/admin/questions/bulk-save', methods=['POST'])
@admin_required
def bulk_save_questions():
    """API endpoint to save reviewed parsed questions into DB and optionally attach to assessment."""
    payload = request.get_json(silent=True) or {}
    questions_data = payload.get('questions', [])
    assessment_id = payload.get('assessment_id')
    section_id = payload.get('section_id')

    if not questions_data:
        return jsonify({'success': False, 'message': 'No questions provided to import.'}), 400

    saved_count = 0
    assessment = db.session.get(Assessment, assessment_id) if assessment_id else None
    current_aq_count = len(assessment.questions) if assessment else 0

    for q_item in questions_data:
        q_text = q_item.get('question_text', '').strip()
        if not q_text:
            continue

        q = Question(
            section_id=section_id if section_id else None,
            question_text=q_text,
            marks=float(q_item.get('marks', 1.0)),
            negative_marks=float(q_item.get('negative_marks', 0.0)),
            time_limit_seconds=int(q_item.get('time_limit_seconds', 60)),
            explanation=q_item.get('explanation', '').strip(),
            difficulty=q_item.get('difficulty', 'medium'),
            category=q_item.get('category', 'General')
        )
        db.session.add(q)
        db.session.flush()

        ans_key = q_item.get('answer', 'A').upper()
        for opt_item in q_item.get('options', []):
            opt_key = opt_item.get('key', '').upper()
            opt_text = opt_item.get('text', '').strip()
            is_corr = (opt_key == ans_key)
            opt = Option(
                question_id=q.id,
                option_key=opt_key,
                option_text=opt_text,
                is_correct=is_corr
            )
            db.session.add(opt)

        if assessment:
            current_aq_count += 1
            aq = AssessmentQuestion(
                assessment_id=assessment.id,
                question_id=q.id,
                order_index=current_aq_count,
                custom_marks=q.marks,
                custom_negative_marks=q.negative_marks,
                custom_time_limit=q.time_limit_seconds
            )
            db.session.add(aq)

        saved_count += 1

    db.session.commit()
    log_admin_action('bulk_import_questions', 'question_bank', None, f'Imported {saved_count} questions')

    msg = f'Successfully imported {saved_count} questions.'
    if assessment:
        msg += f' And attached directly to assessment "{assessment.title}".'
    return jsonify({'success': True, 'count': saved_count, 'message': msg})


# --- Candidates Management ---

@app.route('/admin/candidates')
@admin_required
def admin_candidates():
    candidates = Candidate.query.order_by(Candidate.created_at.desc()).all()
    assessments = Assessment.query.filter_by(status='published').all()
    return render_template(
        'candidates.html',
        candidates=candidates,
        assessments=assessments
    )


@app.route('/admin/candidates/create', methods=['POST'])
@admin_required
def create_candidate():
    candidate_id = request.form.get('candidate_id', '').strip()
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    phone = request.form.get('phone', '').strip()
    department = request.form.get('department', '').strip()
    college = request.form.get('college', '').strip()
    assign_assessment_id = request.form.get('assign_assessment_id')

    if not candidate_id or not name or not email:
        flash('Candidate ID/Roll No, Name and Email are mandatory fields.', 'danger')
        return redirect(url_for('admin_candidates'))

    existing = Candidate.query.filter((Candidate.candidate_id == candidate_id) | (Candidate.email == email)).first()
    if existing:
        flash(f'Candidate with ID "{candidate_id}" or Email "{email}" already exists.', 'warning')
        return redirect(url_for('admin_candidates'))

    cand = Candidate(
        candidate_id=candidate_id,
        name=name,
        email=email,
        phone=phone,
        department=department,
        college=college
    )
    db.session.add(cand)
    db.session.flush()

    if assign_assessment_id:
        token = secrets.token_urlsafe(16)
        assign = Assignment(
            assessment_id=int(assign_assessment_id),
            candidate_id=cand.id,
            access_token=token,
            status='assigned'
        )
        db.session.add(assign)

    db.session.commit()
    log_admin_action('create_candidate', 'candidate', cand.id, f'Registered candidate {name} ({candidate_id})')
    flash(f'Candidate {name} registered successfully.', 'success')
    return redirect(url_for('admin_candidates'))


@app.route('/admin/candidates/import-csv', methods=['POST'])
@admin_required
def import_candidates_csv():
    file = request.files.get('csv_file')
    if not file or not file.filename.endswith('.csv'):
        flash('Please upload a valid .csv file.', 'danger')
        return redirect(url_for('admin_candidates'))

    assign_assessment_id = request.form.get('assign_assessment_id')
    assessment_id = int(assign_assessment_id) if assign_assessment_id else None

    content = file.read().decode('utf-8', errors='ignore')
    reader = csv.DictReader(io.StringIO(content))

    imported = 0
    skipped = 0

    for row in reader:
        c_id = row.get('candidate_id', '').strip()
        name = row.get('name', '').strip()
        email = row.get('email', '').strip()

        if not c_id or not name or not email:
            skipped += 1
            continue

        existing = Candidate.query.filter((Candidate.candidate_id == c_id) | (Candidate.email == email)).first()
        if existing:
            cand = existing
        else:
            cand = Candidate(
                candidate_id=c_id,
                name=name,
                email=email,
                phone=row.get('phone', '').strip(),
                department=row.get('department', '').strip(),
                college=row.get('college', '').strip()
            )
            db.session.add(cand)
            db.session.flush()
            imported += 1

        if assessment_id:
            already_assigned = Assignment.query.filter_by(assessment_id=assessment_id, candidate_id=cand.id).first()
            if not already_assigned:
                token = secrets.token_urlsafe(16)
                assign = Assignment(
                    assessment_id=assessment_id,
                    candidate_id=cand.id,
                    access_token=token,
                    status='assigned'
                )
                db.session.add(assign)

    db.session.commit()
    log_admin_action('bulk_import_candidates', 'candidate', None, f'Imported {imported} candidates (skipped {skipped})')
    flash(f'Import completed: {imported} candidate(s) created, {skipped} skipped.', 'success')
    return redirect(url_for('admin_candidates'))


@app.route('/admin/candidates/<int:id>/delete', methods=['POST'])
@admin_required
def delete_candidate(id):
    cand = db.session.get(Candidate, id)
    if not cand:
        abort(404)
    name = cand.name
    db.session.delete(cand)
    db.session.commit()
    log_admin_action('delete_candidate', 'candidate', id, f'Deleted candidate {name}')
    flash(f'Candidate {name} deleted.', 'success')
    return redirect(url_for('admin_candidates'))


@app.route('/admin/candidates/<int:id>/assignments', methods=['GET'])
@admin_required
def get_candidate_assignments(id):
    cand = db.session.get(Candidate, id)
    if not cand:
        abort(404)
    assignments_data = []
    for a in cand.assignments:
        exam_url = url_for('candidate_exam_entry', token_or_slug=a.access_token, _external=True)
        assignments_data.append({
            'assignment_id': a.id,
            'assessment_title': a.assessment.title if a.assessment else 'Unknown',
            'assessment_code': a.assessment.code if a.assessment else '',
            'status': a.status,
            'access_token': a.access_token,
            'exam_url': exam_url
        })
    return jsonify({'candidate': cand.to_dict(), 'assignments': assignments_data})


# --- Live Monitoring & Anti-Malpractice Dashboard ---

@app.route('/admin/monitoring')
@admin_required
def admin_monitoring():
    return render_template('monitoring.html')


@app.route('/admin/api/monitoring/live')
@admin_required
def api_monitoring_live():
    """Returns live JSON data for real-time monitoring dashboard."""
    active_attempts = Attempt.query.filter_by(status='in_progress').all()
    now = get_utc_now()
    live_list = []

    for att in active_attempts:
        sess = ExamSession.query.filter_by(attempt_id=att.id, is_active=True).order_by(ExamSession.last_heartbeat.desc()).first()
        is_live = False
        last_seen = None
        if sess and sess.last_heartbeat:
            is_live = (now - sess.last_heartbeat).total_seconds() < 90
            last_seen = sess.last_heartbeat.strftime('%H:%M:%S')

        rem_sec = max(0, int((att.due_at - now).total_seconds())) if att.due_at else 0
        recent_viols = [v.to_dict() for v in att.violations.order_by(Violation.recorded_at.desc()).limit(3).all()]

        live_list.append({
            'attempt_id': att.id,
            'candidate_name': att.candidate.name if att.candidate else 'Unknown',
            'candidate_roll': att.candidate.candidate_id if att.candidate else '',
            'candidate_email': att.candidate.email if att.candidate else '',
            'assessment_title': att.assessment.title if att.assessment else '',
            'started_at': att.started_at.strftime('%H:%M:%S') if att.started_at else '',
            'remaining_seconds': rem_sec,
            'current_question': (sess.current_question_index + 1) if sess else 1,
            'total_questions': att.assessment.total_questions if att.assessment else 0,
            'violations_count': att.violation_count,
            'max_violations': att.assessment.max_violations if att.assessment else 3,
            'is_live': is_live,
            'last_heartbeat': last_seen,
            'client_ip': att.client_ip or (sess.ip_address if sess else 'Unknown'),
            'recent_violations': recent_viols
        })

    today_start = get_utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    submissions_today = Attempt.query.filter(
        Attempt.status.in_(['submitted', 'auto_submitted']),
        Attempt.submitted_at >= today_start
    ).count()

    total_violations_today = Violation.query.filter(
        Violation.recorded_at >= today_start
    ).count()

    return jsonify({
        'active_count': len(live_list),
        'submissions_today': submissions_today,
        'total_violations_today': total_violations_today,
        'active_attempts': live_list
    })


@app.route('/admin/api/monitoring/terminate/<int:attempt_id>', methods=['POST'])
@admin_required
def api_monitoring_terminate(attempt_id):
    attempt = db.session.get(Attempt, attempt_id)
    if not attempt:
        abort(404)
    reason = request.json.get('reason', 'Terminated by administrator due to malpractice policy violation.') if request.is_json else 'Terminated by administrator.'

    attempt.status = 'terminated'
    attempt.termination_reason = reason
    evaluate_attempt(attempt)
    db.session.commit()

    log_admin_action('terminate_attempt', 'attempt', attempt.id, f'Terminated exam session for {attempt.candidate.name}: {reason}')
    return jsonify({'success': True, 'message': f'Exam attempt #{attempt_id} has been forcefully terminated.'})


# --- Results & Performance Reports ---

@app.route('/admin/results')
@admin_required
def admin_results():
    assessment_id = request.args.get('assessment_id')
    query = Attempt.query.filter(Attempt.status.in_(['submitted', 'auto_submitted', 'terminated']))

    if assessment_id:
        query = query.filter_by(assessment_id=int(assessment_id))

    attempts = query.order_by(Attempt.percentage.desc(), Attempt.time_taken_seconds.asc()).all()
    assessments = Assessment.query.order_by(Assessment.created_at.desc()).all()

    total_count = len(attempts)
    passed_count = sum(1 for a in attempts if a.passed)
    failed_count = total_count - passed_count
    avg_score = round(sum(a.percentage for a in attempts) / total_count, 1) if total_count > 0 else 0.0
    highest_score = max([a.percentage for a in attempts], default=0.0)
    total_violations = sum(a.violation_count for a in attempts)

    return render_template(
        'results.html',
        attempts=attempts,
        assessments=assessments,
        selected_assessment_id=int(assessment_id) if assessment_id else None,
        total_count=total_count,
        passed_count=passed_count,
        failed_count=failed_count,
        avg_score=avg_score,
        highest_score=highest_score,
        total_violations=total_violations
    )


@app.route('/admin/results/<int:attempt_id>/details')
@admin_required
def admin_result_details(attempt_id):
    """Detailed audit modal endpoint for admin viewing candidate performance and questions breakdown."""
    attempt = db.session.get(Attempt, attempt_id)
    if not attempt:
        abort(404)

    answers_detail = []
    for ans in attempt.answers:
        q = ans.question
        persisted_opt_ids = attempt.get_option_order_for_question(q.id)
        if persisted_opt_ids:
            opt_dict = {o.id: o for o in q.options}
            ordered_opts = [opt_dict[oid] for oid in persisted_opt_ids if oid in opt_dict]
            for o in q.options:
                if o not in ordered_opts:
                    ordered_opts.append(o)
        else:
            ordered_opts = list(q.options)

        selected_key = None
        selected_text = 'Not Answered'
        correct_key = None
        correct_text = None

        for o_idx, opt in enumerate(ordered_opts):
            disp_key = chr(65 + o_idx)
            if ans.selected_option_id and opt.id == ans.selected_option_id:
                selected_key = disp_key
                selected_text = opt.option_text
            if opt.is_correct:
                correct_key = disp_key
                correct_text = opt.option_text

        answers_detail.append({
            'question_id': q.id,
            'question_text': q.question_text,
            'marks': q.marks,
            'negative_marks': q.negative_marks,
            'time_limit': q.time_limit_seconds,
            'selected_key': selected_key,
            'selected_text': selected_text,
            'correct_key': correct_key,
            'correct_text': correct_text,
            'is_correct': ans.is_correct,
            'marks_awarded': round(ans.marks_awarded, 2),
            'time_spent': ans.time_spent_seconds
        })

    violations_detail = [v.to_dict() for v in attempt.violations.order_by(Violation.recorded_at.asc()).all()]

    return jsonify({
        'attempt': attempt.to_dict(),
        'answers': answers_detail,
        'violations': violations_detail
    })


@app.route('/admin/results/export-csv')
@admin_required
def export_results_csv():
    assessment_id = request.args.get('assessment_id')
    query = Attempt.query.filter(Attempt.status.in_(['submitted', 'auto_submitted', 'terminated']))
    if assessment_id:
        query = query.filter_by(assessment_id=int(assessment_id))

    attempts = query.order_by(Attempt.percentage.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Rank', 'Attempt Number', 'Candidate ID', 'Name', 'Email', 'Department', 'College',
        'Assessment', 'Total Marks', 'Obtained Marks', 'Percentage',
        'Pass Status', 'Correct Answers', 'Incorrect Answers', 'Unanswered',
        'Time Taken (Minutes)', 'Violations Count', 'Status', 'Submitted At'
    ])

    for rank, att in enumerate(attempts, 1):
        cand = att.candidate
        time_mins = round(att.time_taken_seconds / 60.0, 1)
        writer.writerow([
            rank,
            att.attempt_number,
            cand.candidate_id if cand else '',
            cand.name if cand else '',
            cand.email if cand else '',
            cand.department if cand else '',
            cand.college if cand else '',
            att.assessment.title if att.assessment else '',
            att.total_marks,
            att.obtained_marks,
            f"{att.percentage}%",
            'PASS' if att.passed else 'FAIL',
            att.correct_count,
            att.incorrect_count,
            att.unanswered_count,
            time_mins,
            att.violation_count,
            att.status.upper(),
            att.submitted_at.strftime('%Y-%m-%d %H:%M:%S') if att.submitted_at else ''
        ])

    filename = f"assesspro_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )


# ---------------------------------------------------------
# CANDIDATE EXAMINATION ENGINE & COMMON SHARED LINKS
# ---------------------------------------------------------

@app.route('/exam/<token_or_slug>')
def candidate_exam_entry(token_or_slug):
    """
    Main Candidate Entry Point.
    Supports BOTH:
    1. Common Assessment Link (e.g. /exam/gse-2026 or /exam/CRT-2026)
    2. Unique Individual Candidate Token (e.g. /exam/xK9z_token)
    """
    token_or_slug_str = str(token_or_slug).strip()

    # 1. Check if token_or_slug is a Common Assessment Slug or Code
    assessment = Assessment.query.filter(
        (Assessment.shared_slug == token_or_slug_str) |
        (db.func.lower(Assessment.code) == token_or_slug_str.lower())
    ).first()

    if assessment:
        return render_common_assessment_portal(assessment)

    # 2. Check if token_or_slug is an individual Assignment token
    assignment = Assignment.query.filter_by(access_token=token_or_slug_str).first()
    if assignment:
        if assignment.status in ('completed', 'terminated'):
            return redirect(url_for('candidate_exam_completed', token=token_or_slug_str))
        return redirect(url_for('candidate_exam_instructions', token=token_or_slug_str))

    flash('Invalid examination link or assessment token.', 'danger')
    return render_template('login.html', error='Invalid assessment invitation link or examination passkey.')


@app.route('/exam/join/<slug>')
def candidate_exam_join(slug):
    """Explicit common examination link endpoint: /exam/join/<slug>"""
    assessment = Assessment.query.filter(
        (Assessment.shared_slug == slug) |
        (db.func.lower(Assessment.code) == slug.lower())
    ).first_or_404()
    return render_common_assessment_portal(assessment)


def render_common_assessment_portal(assessment):
    # Track link visits
    assessment.link_visits_count = (assessment.link_visits_count or 0) + 1
    db.session.commit()

    now = get_utc_now()
    error_notice = None
    is_active = True

    if not assessment.enable_shared_link:
        error_notice = 'This common assessment link has been deactivated by the administrator.'
        is_active = False
    elif assessment.shared_link_active_from and now < assessment.shared_link_active_from:
        error_notice = f'This assessment is not yet active. It opens at {assessment.shared_link_active_from.strftime("%Y-%m-%d %H:%M UTC")}.'
        is_active = False
    elif assessment.shared_link_expires_at and now > assessment.shared_link_expires_at:
        error_notice = f'This assessment link expired at {assessment.shared_link_expires_at.strftime("%Y-%m-%d %H:%M UTC")}.'
        is_active = False

    return render_template(
        'instructions.html',
        mode='common_link',
        assessment=assessment,
        error_notice=error_notice,
        is_active=is_active,
        slug=assessment.shared_slug or assessment.code.lower()
    )


@app.route('/exam/join/<slug>/start', methods=['POST'])
@app.route('/exam/<slug>/start-common', methods=['POST'])
def candidate_common_link_start(slug):
    """
    Handles candidate verification & paper generation from shared link:
    - Verifies access code (if enabled)
    - Resolves candidate profile (or self-registers)
    - Enforces attempt limits and rejects duplicate attempts
    - Generates independent server-side question & option randomization
    """
    assessment = Assessment.query.filter(
        (Assessment.shared_slug == slug) |
        (db.func.lower(Assessment.code) == slug.lower())
    ).first_or_404()

    now = get_utc_now()

    # Link status validation
    if not assessment.enable_shared_link:
        flash('This shared assessment link is currently deactivated.', 'danger')
        return redirect(url_for('candidate_exam_join', slug=slug))

    if assessment.shared_link_active_from and now < assessment.shared_link_active_from:
        flash('This assessment is not yet active.', 'warning')
        return redirect(url_for('candidate_exam_join', slug=slug))

    if assessment.shared_link_expires_at and now > assessment.shared_link_expires_at:
        flash('This assessment link has expired.', 'danger')
        return redirect(url_for('candidate_exam_join', slug=slug))

    # Access Code Verification (if configured)
    if assessment.access_code:
        provided_code = request.form.get('access_code', '').strip()
        if provided_code != assessment.access_code:
            flash('Invalid access code / examination passkey. Please check and try again.', 'danger')
            return redirect(url_for('candidate_exam_join', slug=slug))

    # Candidate details from form
    c_id = request.form.get('candidate_id', '').strip()
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    department = request.form.get('department', '').strip()
    college = request.form.get('college', '').strip()

    if not c_id or not name or not email:
        flash('Roll Number/Candidate ID, Full Name, and Email are required.', 'danger')
        return redirect(url_for('candidate_exam_join', slug=slug))

    # Resolve Candidate
    candidate = Candidate.query.filter((Candidate.candidate_id == c_id) | (Candidate.email == email)).first()
    if not candidate:
        if not assessment.allow_self_registration:
            flash('Self-registration is disabled for this assessment. Please contact the test administrator.', 'danger')
            return redirect(url_for('candidate_exam_join', slug=slug))

        candidate = Candidate(
            candidate_id=c_id,
            name=name,
            email=email,
            department=department,
            college=college
        )
        db.session.add(candidate)
        db.session.flush()
    else:
        if department and not candidate.department:
            candidate.department = department
        if college and not candidate.college:
            candidate.college = college

    # Attempt limits & duplicate checks
    attempts = Attempt.query.filter_by(assessment_id=assessment.id, candidate_id=candidate.id).all()

    # Resume in-progress attempt if exists
    active_attempt = next((a for a in attempts if a.status == 'in_progress'), None)
    if active_attempt:
        if now >= active_attempt.due_at:
            active_attempt.status = 'auto_submitted'
            active_attempt.termination_reason = 'Total examination duration expired.'
            evaluate_attempt(active_attempt)
        else:
            token = active_attempt.assignment.access_token
            session['candidate_attempt_id'] = active_attempt.id
            session['candidate_token'] = token
            flash('Resumed your active examination session.', 'info')
            return redirect(url_for('candidate_exam_take', token=token))

    completed_attempts = [a for a in attempts if a.status in ('submitted', 'auto_submitted', 'terminated')]
    if completed_attempts:
        if not assessment.allow_duplicate_attempts:
            flash('Duplicate attempts are not permitted. You have already completed this examination.', 'warning')
            token = completed_attempts[0].assignment.access_token
            return redirect(url_for('candidate_exam_completed', token=token))

        if len(completed_attempts) >= assessment.max_attempts_per_candidate:
            flash(f'Maximum allowed attempts ({assessment.max_attempts_per_candidate}) reached for this examination.', 'warning')
            token = completed_attempts[0].assignment.access_token
            return redirect(url_for('candidate_exam_completed', token=token))

    # Generate independently randomized question paper & option mapping
    q_order, opt_order = generate_randomized_paper(assessment)
    token = secrets.token_urlsafe(16)

    assignment = Assignment(
        assessment_id=assessment.id,
        candidate_id=candidate.id,
        access_token=token,
        status='in_progress'
    )
    db.session.add(assignment)
    db.session.flush()

    due_at = now + timedelta(minutes=assessment.duration_minutes)

    attempt = Attempt(
        assignment_id=assignment.id,
        candidate_id=candidate.id,
        assessment_id=assessment.id,
        attempt_number=len(completed_attempts) + 1,
        started_at=now,
        due_at=due_at,
        status='in_progress',
        question_order_json=json.dumps(q_order),
        option_order_json=json.dumps(opt_order),
        client_ip=request.remote_addr,
        user_agent=str(request.user_agent.string)[:250]
    )
    db.session.add(attempt)
    db.session.flush()

    for qid in q_order:
        ans = Answer(
            attempt_id=attempt.id,
            question_id=qid,
            is_marked_for_review=False,
            is_answered=False
        )
        db.session.add(ans)

    exam_session = ExamSession(
        attempt_id=attempt.id,
        session_token=secrets.token_hex(16),
        is_active=True,
        last_heartbeat=now,
        ip_address=request.remote_addr,
        user_agent=str(request.user_agent.string)[:250]
    )
    db.session.add(exam_session)
    db.session.commit()

    log_candidate_activity(attempt.id, 'exam_started_common_link', f'Started exam via common link /{slug} from {request.remote_addr}')
    session['candidate_attempt_id'] = attempt.id
    session['candidate_token'] = token

    return redirect(url_for('candidate_exam_take', token=token))


@app.route('/exam/<token>/instructions')
def candidate_exam_instructions(token):
    assignment = Assignment.query.filter_by(access_token=token).first_or_404()
    assessment = assignment.assessment
    candidate = assignment.candidate

    if assignment.status in ('completed', 'terminated'):
        return redirect(url_for('candidate_exam_completed', token=token))

    return render_template(
        'instructions.html',
        mode='individual',
        assignment=assignment,
        assessment=assessment,
        candidate=candidate,
        token=token
    )


@app.route('/exam/<token>/start', methods=['POST'])
def candidate_exam_start(token):
    assignment = Assignment.query.filter_by(access_token=token).first_or_404()
    assessment = assignment.assessment
    candidate = assignment.candidate

    if assignment.status in ('completed', 'terminated'):
        return redirect(url_for('candidate_exam_completed', token=token))

    attempt = Attempt.query.filter_by(assignment_id=assignment.id).first()
    now = get_utc_now()

    if not attempt:
        due_at = now + timedelta(minutes=assessment.duration_minutes)

        # Generate independently randomized paper on server
        q_order, opt_order = generate_randomized_paper(assessment)

        attempt = Attempt(
            assignment_id=assignment.id,
            candidate_id=candidate.id,
            assessment_id=assessment.id,
            attempt_number=1,
            started_at=now,
            due_at=due_at,
            status='in_progress',
            question_order_json=json.dumps(q_order),
            option_order_json=json.dumps(opt_order),
            client_ip=request.remote_addr,
            user_agent=str(request.user_agent.string)[:250]
        )
        db.session.add(attempt)
        db.session.flush()

        for qid in q_order:
            ans = Answer(
                attempt_id=attempt.id,
                question_id=qid,
                is_marked_for_review=False,
                is_answered=False
            )
            db.session.add(ans)

        assignment.status = 'in_progress'

    sess_token = secrets.token_hex(16)
    exam_session = ExamSession(
        attempt_id=attempt.id,
        session_token=sess_token,
        is_active=True,
        last_heartbeat=now,
        ip_address=request.remote_addr,
        user_agent=str(request.user_agent.string)[:250]
    )
    db.session.add(exam_session)
    db.session.commit()

    log_candidate_activity(attempt.id, 'exam_started', f'Assessment session commenced from {request.remote_addr}')
    session['candidate_attempt_id'] = attempt.id
    session['candidate_token'] = token

    return redirect(url_for('candidate_exam_take', token=token))


@app.route('/exam/<token>/take')
def candidate_exam_take(token):
    assignment = Assignment.query.filter_by(access_token=token).first_or_404()
    attempt = Attempt.query.filter_by(assignment_id=assignment.id).first()

    if not attempt or attempt.status in ('submitted', 'auto_submitted', 'terminated'):
        return redirect(url_for('candidate_exam_completed', token=token))

    return render_template(
        'exam.html',
        assignment=assignment,
        assessment=assignment.assessment,
        candidate=assignment.candidate,
        attempt=attempt,
        token=token
    )


@app.route('/api/exam/<token>/state')
def api_exam_state(token):
    """
    Returns full state of the exam to Vanilla JS exam engine:
    - Authoritative remaining countdown from server.
    - Questions and options in candidate's persisted randomized order.
    - NEVER reveals correct answers or evaluation keys.
    """
    assignment = Assignment.query.filter_by(access_token=token).first_or_404()
    attempt = Attempt.query.filter_by(assignment_id=assignment.id).first_or_404()
    assessment = assignment.assessment

    now = get_utc_now()

    if now >= attempt.due_at or attempt.status in ('submitted', 'auto_submitted', 'terminated'):
        if attempt.status == 'in_progress':
            attempt.status = 'auto_submitted'
            attempt.termination_reason = 'Total examination duration expired.'
            evaluate_attempt(attempt)
        return jsonify({
            'expired': True,
            'redirect': url_for('candidate_exam_completed', token=token)
        })

    remaining_seconds = max(0, int((attempt.due_at - now).total_seconds()))

    # Build question order
    q_ids = attempt.get_question_ids()
    questions_list = []
    aq_map = {aq.question_id: aq for aq in assessment.questions}
    questions_by_id = {q.id: q for q in Question.query.filter(Question.id.in_(q_ids)).all()}

    for idx, qid in enumerate(q_ids, 1):
        q = questions_by_id.get(qid)
        if not q:
            continue
        aq = aq_map.get(qid)

        # Options in attempt-specific shuffled order
        persisted_opt_ids = attempt.get_option_order_for_question(qid)
        if persisted_opt_ids:
            opt_dict = {opt.id: opt for opt in q.options}
            ordered_opts = [opt_dict[oid] for oid in persisted_opt_ids if oid in opt_dict]
            for opt in q.options:
                if opt not in ordered_opts:
                    ordered_opts.append(opt)
        else:
            ordered_opts = list(q.options)

        options = []
        for o_idx, opt in enumerate(ordered_opts):
            disp_key = chr(65 + o_idx)
            options.append({
                'id': opt.id,
                'key': disp_key,
                'text': opt.option_text
            })

        questions_list.append({
            'id': q.id,
            'index': idx,
            'text': q.question_text,
            'marks': aq.effective_marks if aq else q.marks,
            'negative_marks': aq.effective_negative_marks if aq else q.negative_marks,
            'time_limit_seconds': aq.effective_time_limit if aq else q.time_limit_seconds,
            'section': q.section.name if q.section else 'General',
            'difficulty': q.difficulty,
            'options': options
        })

    saved_answers = {}
    for ans in attempt.answers:
        saved_answers[ans.question_id] = {
            'selected_option_id': ans.selected_option_id,
            'is_marked_for_review': ans.is_marked_for_review,
            'is_answered': ans.is_answered,
            'time_spent': ans.time_spent_seconds
        }

    return jsonify({
        'expired': False,
        'remaining_seconds': remaining_seconds,
        'assessment': {
            'title': assessment.title,
            'code': assessment.code,
            'duration_minutes': assessment.duration_minutes,
            'enable_question_timer': assessment.enable_question_timer,
            'default_question_time': assessment.default_question_time,
            'allow_backtracking': assessment.allow_backtracking,
            'max_violations': assessment.max_violations,
            'strict_fullscreen': assessment.strict_fullscreen,
            'block_copy_paste': assessment.block_copy_paste
        },
        'candidate': {
            'name': assignment.candidate.name,
            'candidate_id': assignment.candidate.candidate_id,
            'email': assignment.candidate.email
        },
        'questions': questions_list,
        'saved_answers': saved_answers,
        'violation_count': attempt.violation_count,
        'max_violations': assessment.max_violations
    })


@app.route('/api/exam/<token>/save-answer', methods=['POST'])
def api_exam_save_answer(token):
    assignment = Assignment.query.filter_by(access_token=token).first_or_404()
    attempt = Attempt.query.filter_by(assignment_id=assignment.id).first_or_404()

    now = get_utc_now()
    if now > attempt.due_at or attempt.status != 'in_progress':
        return jsonify({'success': False, 'message': 'Exam expired or already finalized.'}), 400

    data = request.get_json(silent=True) or {}
    question_id = data.get('question_id')
    selected_option_id = data.get('selected_option_id')
    is_review = data.get('is_marked_for_review', False)
    time_spent = data.get('time_spent_seconds', 0)

    if not question_id:
        return jsonify({'success': False, 'message': 'Question ID missing.'}), 400

    ans = Answer.query.filter_by(attempt_id=attempt.id, question_id=question_id).first()
    if not ans:
        ans = Answer(attempt_id=attempt.id, question_id=question_id)
        db.session.add(ans)

    ans.selected_option_id = selected_option_id if selected_option_id else None
    ans.is_answered = bool(selected_option_id)
    ans.is_marked_for_review = bool(is_review)
    if time_spent:
        ans.time_spent_seconds = ans.time_spent_seconds + int(time_spent)

    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/exam/<token>/heartbeat', methods=['POST'])
def api_exam_heartbeat(token):
    assignment = Assignment.query.filter_by(access_token=token).first_or_404()
    attempt = Attempt.query.filter_by(assignment_id=assignment.id).first_or_404()

    data = request.get_json(silent=True) or {}
    q_idx = data.get('current_question_index', 0)

    now = get_utc_now()

    if now >= attempt.due_at and attempt.status == 'in_progress':
        attempt.status = 'auto_submitted'
        attempt.termination_reason = 'Total examination duration expired.'
        evaluate_attempt(attempt)
        return jsonify({
            'active': False,
            'expired': True,
            'redirect': url_for('candidate_exam_completed', token=token)
        })

    sess = ExamSession.query.filter_by(attempt_id=attempt.id, is_active=True).first()
    if sess:
        sess.last_heartbeat = now
        sess.current_question_index = q_idx
    else:
        sess = ExamSession(
            attempt_id=attempt.id,
            session_token=secrets.token_hex(16),
            is_active=True,
            last_heartbeat=now,
            current_question_index=q_idx,
            ip_address=request.remote_addr,
            user_agent=str(request.user_agent.string)[:250]
        )
        db.session.add(sess)

    db.session.commit()
    remaining = max(0, int((attempt.due_at - now).total_seconds()))
    return jsonify({'active': attempt.status == 'in_progress', 'remaining_seconds': remaining})


@app.route('/api/exam/<token>/violation', methods=['POST'])
def api_exam_violation(token):
    assignment = Assignment.query.filter_by(access_token=token).first_or_404()
    attempt = Attempt.query.filter_by(assignment_id=assignment.id).first_or_404()

    if attempt.status != 'in_progress':
        return jsonify({'terminated': True, 'redirect': url_for('candidate_exam_completed', token=token)})

    data = request.get_json(silent=True) or {}
    v_type = data.get('violation_type', 'unspecified')
    desc = data.get('description', 'Anti-malpractice trigger recorded.')
    q_idx = data.get('question_index', 0)

    violation = Violation(
        attempt_id=attempt.id,
        violation_type=v_type,
        description=desc,
        question_index=q_idx
    )
    db.session.add(violation)
    attempt.violation_count += 1
    db.session.commit()

    max_v = attempt.assessment.max_violations
    terminated = False

    if attempt.violation_count >= max_v:
        attempt.status = 'auto_submitted'
        attempt.termination_reason = f'Terminated: Exceeded maximum allowed malpractice violations ({attempt.violation_count}/{max_v}).'
        evaluate_attempt(attempt)
        terminated = True
        log_candidate_activity(attempt.id, 'exam_terminated_malpractice', attempt.termination_reason)

    return jsonify({
        'violation_count': attempt.violation_count,
        'max_violations': max_v,
        'terminated': terminated,
        'redirect': url_for('candidate_exam_completed', token=token) if terminated else None
    })


@app.route('/api/exam/<token>/submit', methods=['POST'])
def api_exam_submit(token):
    assignment = Assignment.query.filter_by(access_token=token).first_or_404()
    attempt = Attempt.query.filter_by(assignment_id=assignment.id).first_or_404()

    if attempt.status == 'in_progress':
        attempt.status = 'submitted'
        evaluate_attempt(attempt)
        log_candidate_activity(attempt.id, 'exam_submitted_normal', 'Candidate submitted assessment')

    return jsonify({
        'success': True,
        'redirect': url_for('candidate_exam_completed', token=token)
    })


@app.route('/exam/<token>/completed')
def candidate_exam_completed(token):
    assignment = Assignment.query.filter_by(access_token=token).first_or_404()
    attempt = Attempt.query.filter_by(assignment_id=assignment.id).first()

    return render_template(
        'results.html',
        mode='candidate_completed',
        assignment=assignment,
        assessment=assignment.assessment,
        candidate=assignment.candidate,
        attempt=attempt
    )


# ---------------------------------------------------------
# APPLICATION ENTRY POINT & SEED RUNNER
# ---------------------------------------------------------

with app.app_context():
    try:
        seed_default_data()
    except Exception as e:
        app.logger.warning(f"Database initialization deferred (set TURSO_AUTH_TOKEN to seed): {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
