import pytest
from django.contrib.auth.models import User
from django.urls import reverse

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class TestSignup:
    def test_valid_signup_creates_user_and_logs_in(self, client):
        response = client.post(
            reverse("signup"),
            {
                "username": "alice",
                "password1": VALID_PASSWORD,
                "password2": VALID_PASSWORD,
            },
        )

        assert response.status_code == 302
        assert response.url == reverse("home")
        user = User.objects.get(username="alice")
        assert int(client.session["_auth_user_id"]) == user.pk

    def test_short_password_reshows_form_with_specific_error(self, client):
        response = client.post(
            reverse("signup"),
            {"username": "bob", "password1": "short1", "password2": "short1"},
        )

        assert response.status_code == 200
        assert not User.objects.filter(username="bob").exists()
        assert "too short" in response.content.decode().lower()

    def test_password_similar_to_username_reshows_form_with_specific_error(
        self, client
    ):
        response = client.post(
            reverse("signup"),
            {
                "username": "carol12345",
                "password1": "carol12345",
                "password2": "carol12345",
            },
        )

        assert response.status_code == 200
        assert not User.objects.filter(username="carol12345").exists()
        assert "too similar" in response.content.decode().lower()

    def test_duplicate_username_shows_error_and_does_not_duplicate(
        self, client, django_user_model
    ):
        django_user_model.objects.create_user(
            username="dave", password=VALID_PASSWORD
        )

        response = client.post(
            reverse("signup"),
            {
                "username": "dave",
                "password1": VALID_PASSWORD,
                "password2": VALID_PASSWORD,
            },
        )

        assert response.status_code == 200
        assert User.objects.filter(username="dave").count() == 1
        assert "already exists" in response.content.decode().lower()

    def test_authenticated_user_redirected_away_from_signup(
        self, client, django_user_model
    ):
        user = django_user_model.objects.create_user(
            username="erin", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("signup"))

        assert response.status_code == 302
        assert response.url == reverse("home")


@pytest.mark.django_db
class TestLogin:
    def test_correct_credentials_log_in_and_redirect_home(
        self, client, django_user_model
    ):
        django_user_model.objects.create_user(
            username="frank", password=VALID_PASSWORD
        )

        response = client.post(
            reverse("login"), {"username": "frank", "password": VALID_PASSWORD}
        )

        assert response.status_code == 302
        assert response.url == reverse("home")
        assert "_auth_user_id" in client.session

    def test_wrong_password_shows_generic_error_and_stays_unauthenticated(
        self, client, django_user_model
    ):
        django_user_model.objects.create_user(
            username="grace", password=VALID_PASSWORD
        )

        response = client.post(
            reverse("login"), {"username": "grace", "password": "wrong-password"}
        )

        assert response.status_code == 200
        assert "_auth_user_id" not in client.session
        assert "please enter a correct" in response.content.decode().lower()

    def test_unknown_username_shows_same_generic_error(self, client):
        response = client.post(
            reverse("login"), {"username": "ghost", "password": "whatever123"}
        )

        assert response.status_code == 200
        assert "_auth_user_id" not in client.session
        assert "please enter a correct" in response.content.decode().lower()

    def test_authenticated_user_redirected_away_from_login(
        self, client, django_user_model
    ):
        user = django_user_model.objects.create_user(
            username="henry", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("login"))

        assert response.status_code == 302
        assert response.url == reverse("home")


@pytest.mark.django_db
class TestLogout:
    def test_logout_requires_post(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ivy", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("logout"))

        assert response.status_code == 405
        assert "_auth_user_id" in client.session

    def test_logout_clears_the_session(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="jack", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.post(reverse("logout"))

        assert response.status_code == 302
        assert "_auth_user_id" not in client.session


@pytest.mark.django_db
@pytest.mark.urls("tests.urls_protected")
class TestLoginRequiredIntegration:
    def test_signup_then_login_required_check_succeeds_without_separate_login(
        self, client
    ):
        client.post(
            reverse("signup"),
            {
                "username": "kim",
                "password1": VALID_PASSWORD,
                "password2": VALID_PASSWORD,
            },
        )

        response = client.get(reverse("protected"))

        assert response.status_code == 200
        assert response.content == b"protected"

    def test_after_logout_login_required_check_redirects_to_login(
        self, client, django_user_model
    ):
        user = django_user_model.objects.create_user(
            username="liam", password=VALID_PASSWORD
        )
        client.force_login(user)
        client.post(reverse("logout"))

        response = client.get(reverse("protected"))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))
