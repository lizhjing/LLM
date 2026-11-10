"""get_pet 工具测试：参数校验、后端错误、成功路径。"""

from unittest.mock import AsyncMock

import pytest

from pet_hospital_mcp.errors import BackendAPIError, BackendTimeoutError
from pet_hospital_mcp.tools.get_pet import execute_get_pet

_PET = {"id": "PET-000001", "name": "旺财", "species": "犬"}


@pytest.mark.asyncio
async def test_missing_pet_id_returns_validation_error() -> None:
    client = AsyncMock()
    result = await execute_get_pet(client, "")
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "VALIDATION_ERROR"
    client.get_pet.assert_not_called()


@pytest.mark.asyncio
async def test_non_string_pet_id_returns_validation_error() -> None:
    client = AsyncMock()
    result = await execute_get_pet(client, 123)
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_backend_timeout_returns_backend_timeout() -> None:
    client = AsyncMock()
    client.get_pet.side_effect = BackendTimeoutError("timeout")
    result = await execute_get_pet(client, "PET-000001")
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "BACKEND_TIMEOUT"


@pytest.mark.asyncio
async def test_backend_api_error_returns_backend_api_error() -> None:
    client = AsyncMock()
    client.get_pet.side_effect = BackendAPIError("not found")
    result = await execute_get_pet(client, "PET-999")
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "BACKEND_API_ERROR"


@pytest.mark.asyncio
async def test_success_returns_pet_object() -> None:
    client = AsyncMock()
    client.get_pet.return_value = {"code": 0, "data": _PET}
    result = await execute_get_pet(client, "PET-000001")
    assert result.is_error is False
    assert result.structured_content["id"] == "PET-000001"
    assert result.structured_content["name"] == "旺财"
