from datetime import datetime, timedelta, timezone

from yysls_news.delivery.qqbot.gateway import _extract_binding_event
from yysls_news.domain.models import MessageMode, SceneType
from yysls_news.services.qq_binding import BINDING_CODE_TTL_SECONDS, QQBindingService
from yysls_news.storage.database import Database
from yysls_news.storage.repositories import DeliveryTargetRepository, QQBindingRepository


def _service(tmp_path) -> tuple[Database, QQBindingService]:
    database = Database(tmp_path / "qq-binding.db")
    database.initialize()
    return database, QQBindingService(
        QQBindingRepository(database), DeliveryTargetRepository(database)
    )


def test_binding_code_is_one_time_and_creates_target(tmp_path) -> None:
    database, service = _service(tmp_path)

    binding = service.create(SceneType.GROUP, MessageMode.IMAGE)

    assert len(binding.code) == 8
    assert BINDING_CODE_TTL_SECONDS == 180
    assert service.get_status(binding.id)["status"] == "pending"

    result = service.consume(SceneType.GROUP, binding.code, "group-openid")
    assert result is not None
    assert result.target_openid == "group-openid"
    assert service.consume(SceneType.GROUP, binding.code, "another-openid") is None
    assert service.get_status(binding.id)["status"] == "bound"

    targets = DeliveryTargetRepository(database).list_all()
    assert len(targets) == 1
    assert targets[0]["scene_type"] == "group"
    assert targets[0]["target_openid"] == "group-openid"
    assert targets[0]["message_mode"] == "image"


def test_binding_code_expires_and_scene_must_match(tmp_path) -> None:
    database, service = _service(tmp_path)
    binding = service.create(SceneType.USER)

    assert service.consume(SceneType.GROUP, binding.code, "group-openid") is None
    with database.connect() as connection:
        connection.execute(
            "UPDATE qq_binding_codes SET expires_at = ? WHERE id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                binding.id,
            ),
        )

    assert service.get_status(binding.id)["status"] == "expired"
    assert service.consume(SceneType.USER, binding.code, "user-openid") is None


def test_gateway_extracts_user_and_group_openid() -> None:
    user_event = _extract_binding_event(
        "C2C_MESSAGE_CREATE",
        {"content": "  ABCD2345 ", "author": {"user_openid": "user-openid"}},
    )
    group_event = _extract_binding_event(
        "GROUP_AT_MESSAGE_CREATE",
        {"content": "ABCD2345", "group_openid": "group-openid"},
    )

    assert user_event == (SceneType.USER, "ABCD2345", "user-openid")
    assert group_event == (SceneType.GROUP, "ABCD2345", "group-openid")
