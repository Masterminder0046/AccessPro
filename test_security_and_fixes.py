import os
import re
import unittest
from werkzeug.security import generate_password_hash, check_password_hash

# Configure testing environment before importing app
os.environ['TESTING'] = 'True'
os.environ['INITIAL_ADMIN_USERNAME'] = 'superadmin'
os.environ['INITIAL_ADMIN_PASSWORD'] = 'SecurePass_2026!xyz'
os.environ['INITIAL_ADMIN_EMAIL'] = 'admin@assesspro.test'

from app import app, seed_default_data
from database import db, User, Assessment, Question, Option, Section, Candidate, Assignment, Attempt, Answer


class AssessProFixesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        with app.app_context():
            db.create_all()
            sec = Section.query.filter_by(name='Technical & Programming').first()
            if not sec:
                sec = Section(name='Technical & Programming', description='Core Tech', order_index=1)
                db.session.add(sec)
                db.session.flush()

            admin = User.query.filter_by(username='superadmin').first()
            if not admin:
                admin = User(
                    username='superadmin',
                    email='admin@assesspro.test',
                    role='admin',
                    full_name='System Administrator'
                )
                admin.set_password('SecurePass_2026!xyz')
                db.session.add(admin)
                db.session.flush()

            asm = Assessment.query.filter_by(code='GSE-2026-T1').first()
            if not asm:
                asm = Assessment(
                    title='Graduate Software Engineer Assessment 2026',
                    code='GSE-2026-T1',
                    shared_slug='gse-2026',
                    enable_shared_link=True,
                    description='Recruitment test for software engineers',
                    instructions='Follow all rules.',
                    duration_minutes=45,
                    passing_percentage=60.0,
                    randomize_questions=True,
                    randomize_options=True,
                    max_attempts_per_candidate=1,
                    allow_self_registration=True,
                    allow_duplicate_attempts=False,
                    status='published',
                    created_by_id=admin.id
                )
                db.session.add(asm)
                db.session.flush()

            q = Question.query.first()
            if not q:
                q = Question(
                    section_id=sec.id,
                    question_text='What is a balanced binary search tree?',
                    marks=2.0,
                    negative_marks=0.5,
                    time_limit_seconds=45,
                    explanation='AVL or Red-Black Tree provides O(log n) operations.'
                )
                db.session.add(q)
                db.session.flush()

                opt1 = Option(question_id=q.id, option_key='A', option_text='AVL Tree', is_correct=True)
                opt2 = Option(question_id=q.id, option_key='B', option_text='Linked List', is_correct=False)
                db.session.add_all([opt1, opt2])

            db.session.commit()

    def setUp(self):
        self.client = app.test_client()

    # -------------------------------------------------------------
    # 1. Admin Credentials Exposure Tests
    # -------------------------------------------------------------
    def test_login_page_has_no_credentials_exposed(self):
        """Verify login page has no visible credentials, pre-filled values, or demo credentials note."""
        res = self.client.get('/login')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')

        # Never contain admin123
        self.assertNotIn('admin123', html)

        # Inputs must not be pre-filled
        self.assertNotIn('value="admin"', html)
        self.assertNotIn("value='admin'", html)
        self.assertNotIn('value="admin123"', html)
        self.assertNotIn("value='admin123'", html)

        # No default credentials text
        self.assertNotIn('Default demo credentials', html)
        self.assertIn('placeholder="••••••••"', html)

    # -------------------------------------------------------------
    # 2. Route & Access Security Tests
    # -------------------------------------------------------------
    def test_unauthenticated_admin_routes_redirect_to_login(self):
        """Verify unauthenticated requests to admin HTML pages redirect to /login."""
        protected_routes = [
            '/admin',
            '/admin/dashboard',
            '/admin/assessments',
            '/admin/candidates',
            '/admin/questions',
            '/admin/monitoring',
            '/admin/results',
            '/admin/results/export-csv'
        ]
        for route in protected_routes:
            with self.subTest(route=route):
                res = self.client.get(route)
                self.assertEqual(res.status_code, 302)
                self.assertIn('/login', res.headers.get('Location', ''))

    def test_unauthenticated_admin_api_returns_401(self):
        """Verify unauthenticated requests to admin API routes return HTTP 401."""
        res = self.client.get('/admin/api/monitoring/live')
        self.assertEqual(res.status_code, 401)
        data = res.get_json()
        self.assertIn('Unauthorized', data.get('error', ''))

    # -------------------------------------------------------------
    # 3. Student Assessment Link Tests (Incognito / Unauthenticated)
    # -------------------------------------------------------------
    def test_shared_assessment_url_opens_student_interface(self):
        """Verify /assessment/<slug> opens student page (instructions & candidate registration)."""
        res = self.client.get('/assessment/gse-2026')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')

        # Must display student/candidate interface
        self.assertIn('Candidate Registration & Access Verification', html)
        self.assertIn('Candidate ID / Roll Number', html)
        self.assertIn('Full Legal Name', html)
        self.assertIn('Email Address', html)
        self.assertIn('Generate Paper & Start Assessment', html)

        # Must NOT display admin navigation or admin login
        self.assertNotIn('Administrator Username or Email', html)
        self.assertNotIn('Sign In to Portal', html)
        self.assertNotIn('Live Monitoring', html)
        self.assertNotIn('Question Bank', html)

    def test_legacy_exam_url_also_opens_student_interface(self):
        """Verify legacy route /exam/<slug> continues to work and opens student page."""
        res = self.client.get('/exam/gse-2026')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')
        self.assertIn('Candidate Registration & Access Verification', html)
        self.assertNotIn('Sign In to Portal', html)

    def test_invalid_assessment_link_renders_candidate_error_page(self):
        """Verify invalid assessment link does NOT render login.html or leak admin portal."""
        res = self.client.get('/assessment/completely-invalid-token-12345')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')

        # Displays candidate-facing error page
        self.assertIn('Invalid or Expired Assessment Link', html)
        self.assertIn('Invalid assessment invitation link or examination passkey', html)

        # Must NEVER render admin login or leak admin credentials
        self.assertNotIn('Administrator Username or Email', html)
        self.assertNotIn('Sign In to Portal', html)
        self.assertNotIn('admin123', html)

    def test_draft_assessment_link_is_inactive(self):
        """Verify non-published assessment indicates link inactive to students."""
        with app.app_context():
            draft_asm = Assessment.query.filter_by(code='DRAFT-2026').first()
            if not draft_asm:
                draft_asm = Assessment(
                    title='Draft Assessment 2026',
                    code='DRAFT-2026',
                    shared_slug='draft-2026',
                    enable_shared_link=True,
                    status='draft'
                )
                db.session.add(draft_asm)
                db.session.commit()

        res = self.client.get('/assessment/draft-2026')
        self.assertEqual(res.status_code, 200)
        html = res.data.decode('utf-8')
        self.assertIn('Link Inactive', html)
        self.assertIn('This assessment is currently not active or is in draft status', html)
        self.assertNotIn('Generate Paper & Start Assessment', html)

    # -------------------------------------------------------------
    # 4. Candidate Registration, Paper Generation & Exam Flow
    # -------------------------------------------------------------
    def test_candidate_exam_flow_and_submission(self):
        """Test candidate registration, taking exam, saving answers, submitting, and completion receipt."""
        # 1. Candidate registers via common link
        form_data = {
            'candidate_id': 'TEST-CAND-001',
            'name': 'Test Student',
            'email': 'student@example.com',
            'department': 'Computer Science',
            'college': 'Engineering College'
        }
        res = self.client.post('/assessment/join/gse-2026/start', data=form_data, follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        redirect_url = res.headers.get('Location')
        self.assertIn('/assessment/', redirect_url)
        self.assertIn('/take', redirect_url)

        # Extract token
        match = re.search(r'/assessment/([^/]+)/take', redirect_url)
        self.assertTrue(match, "Token should be present in take URL")
        token = match.group(1)

        # 2. Access exam take page
        res = self.client.get(f'/assessment/{token}/take')
        self.assertEqual(res.status_code, 200)

        # 3. Candidate fetches exam state via API
        state_res = self.client.get(f'/api/assessment/{token}/state')
        self.assertEqual(state_res.status_code, 200)
        state_data = state_res.get_json()
        self.assertFalse(state_data['expired'])
        self.assertGreaterEqual(len(state_data['questions']), 1)
        q = state_data['questions'][0]

        # Verify question data NEVER reveals correct answers or explanations
        self.assertNotIn('is_correct', str(q))
        self.assertNotIn('explanation', q)
        for opt in q['options']:
            self.assertNotIn('is_correct', opt)

        # 4. Candidate saves answer
        chosen_opt_id = q['options'][0]['id']
        ans_res = self.client.post(
            f'/api/assessment/{token}/save-answer',
            json={'question_id': q['id'], 'selected_option_id': chosen_opt_id, 'time_spent_seconds': 15}
        )
        self.assertEqual(ans_res.status_code, 200)
        self.assertTrue(ans_res.get_json()['success'])

        # 5. Candidate submits exam
        sub_res = self.client.post(f'/api/assessment/{token}/submit')
        self.assertEqual(sub_res.status_code, 200)
        self.assertTrue(sub_res.get_json()['success'])
        self.assertIn(f'/assessment/{token}/completed', sub_res.get_json()['redirect'])

        # 6. Candidate views completed page
        comp_res = self.client.get(f'/assessment/{token}/completed')
        self.assertEqual(comp_res.status_code, 200)
        comp_html = comp_res.data.decode('utf-8')
        self.assertIn('Examination Submitted Successfully', comp_html)
        self.assertIn('Test Student', comp_html)
        self.assertIn('TEST-CAND-001', comp_html)
        self.assertIn('COMPLETED', comp_html)
        # Does NOT reveal answer keys or admin results table
        self.assertNotIn('Export CSV Report', comp_html)
        self.assertNotIn('Total Submissions', comp_html)

        # 7. Duplicate attempt prevention
        dup_res = self.client.post('/assessment/join/gse-2026/start', data=form_data, follow_redirects=True)
        dup_html = dup_res.data.decode('utf-8')
        self.assertIn('Duplicate attempts are not permitted', dup_html)

    # -------------------------------------------------------------
    # 5. Access Code / Passkey Validation Test
    # -------------------------------------------------------------
    def test_assessment_access_code_enforcement(self):
        """Verify access code passkey is strictly required and validated when configured."""
        with app.app_context():
            passkey_asm = Assessment.query.filter_by(code='CODE-2026').first()
            if not passkey_asm:
                passkey_asm = Assessment(
                    title='Passkey Protected Exam',
                    code='CODE-2026',
                    shared_slug='code-2026',
                    access_code='SECRET888',
                    enable_shared_link=True,
                    status='published'
                )
                db.session.add(passkey_asm)
                db.session.commit()

        # 1. Attempt with invalid access code
        res_wrong = self.client.post('/assessment/join/code-2026/start', data={
            'candidate_id': 'CAND-PASS-1',
            'name': 'Passkey User',
            'email': 'passkey@example.com',
            'access_code': 'WRONGPASS'
        }, follow_redirects=True)
        self.assertIn('Invalid access code / examination passkey', res_wrong.data.decode('utf-8'))

        # 2. Attempt with correct access code
        res_correct = self.client.post('/assessment/join/code-2026/start', data={
            'candidate_id': 'CAND-PASS-1',
            'name': 'Passkey User',
            'email': 'passkey@example.com',
            'access_code': 'SECRET888'
        }, follow_redirects=False)
        self.assertEqual(res_correct.status_code, 302)
        self.assertIn('/take', res_correct.headers.get('Location'))

    # -------------------------------------------------------------
    # 6. Individual Candidate Assignment Token Flow
    # -------------------------------------------------------------
    def test_individual_candidate_assignment_token_flow(self):
        """Verify individual assigned token URL /assessment/<token> opens candidate instructions."""
        with app.app_context():
            cand = Candidate.query.filter_by(candidate_id='INDIV-001').first()
            if not cand:
                cand = Candidate(
                    candidate_id='INDIV-001',
                    name='Individual Candidate',
                    email='individual@example.com'
                )
                db.session.add(cand)
                db.session.flush()

            asm = Assessment.query.filter_by(code='GSE-2026-T1').first()
            assignment = Assignment(
                assessment_id=asm.id,
                candidate_id=cand.id,
                access_token='secure-individual-token-xyz789',
                status='assigned'
            )
            db.session.add(assignment)
            db.session.commit()

        # Candidate visits individual token link
        res = self.client.get('/assessment/secure-individual-token-xyz789')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/instructions', res.headers.get('Location'))

        inst_res = self.client.get('/assessment/secure-individual-token-xyz789/instructions')
        self.assertEqual(inst_res.status_code, 200)
        html = inst_res.data.decode('utf-8')
        self.assertIn('Individual Candidate', html)
        self.assertIn('INDIV-001', html)
        self.assertIn('Start Examination', html)

    # -------------------------------------------------------------
    # 7. Database Seeding Does NOT Overwrite Passwords
    # -------------------------------------------------------------
    def test_seeding_does_not_overwrite_admin_password(self):
        """Verify seed_default_data does not reset admin password or recreate default credentials."""
        with app.app_context():
            admin = User.query.filter_by(username='superadmin').first()
            admin.set_password('MyCustomChangedPassword_123!')
            db.session.commit()

            # Run seed_default_data
            seed_default_data()

            # Verify password remains the custom password
            refreshed_admin = User.query.filter_by(username='superadmin').first()
            self.assertTrue(refreshed_admin.check_password('MyCustomChangedPassword_123!'))
            # Reset back for other tests
            refreshed_admin.set_password('SecurePass_2026!xyz')
            db.session.commit()

    # -------------------------------------------------------------
    # 8. Admin Authentication & Session Separation
    # -------------------------------------------------------------
    def test_admin_login_and_session_separation(self):
        """Test admin login, access to admin dashboard, and session isolation."""
        # Failed login with wrong password
        fail_res = self.client.post('/login', data={'username': 'superadmin', 'password': 'wrongpassword'})
        self.assertEqual(fail_res.status_code, 200)
        self.assertIn('Invalid administrator credentials', fail_res.data.decode('utf-8'))

        # Successful login
        login_res = self.client.post(
            '/login',
            data={'username': 'superadmin', 'password': 'SecurePass_2026!xyz'},
            follow_redirects=False
        )
        self.assertEqual(login_res.status_code, 302)
        self.assertIn('/admin/dashboard', login_res.headers.get('Location'))

        # Now admin routes are accessible
        dash_res = self.client.get('/admin/dashboard')
        self.assertEqual(dash_res.status_code, 200)
        self.assertIn('Dashboard Overview', dash_res.data.decode('utf-8'))

        # Admin creates an assessment and receives /assessment/<slug> link
        create_res = self.client.post('/admin/assessments/create', data={
            'title': 'New Placement Exam',
            'code': 'NPE-2026',
            'shared_slug': 'npe-2026',
            'duration_minutes': 60,
            'enable_shared_link': 'on'
        }, follow_redirects=True)
        self.assertEqual(create_res.status_code, 200)
        create_html = create_res.data.decode('utf-8')
        self.assertIn('/assessment/npe-2026', create_html)

        # Admin logs out
        logout_res = self.client.get('/logout', follow_redirects=True)
        self.assertEqual(logout_res.status_code, 200)
        self.assertIn('successfully logged out', logout_res.data.decode('utf-8'))

        # Dashboard no longer accessible
        dash_after = self.client.get('/admin/dashboard')
        self.assertEqual(dash_after.status_code, 302)
        self.assertIn('/login', dash_after.headers.get('Location'))


if __name__ == '__main__':
    unittest.main()
