from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class FileEntry(StrictModel):
    path: str
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    size: int = Field(ge=0)


class Manifest(StrictModel):
    schema_version: Literal['1.0']
    package_type: Literal['ocr_model', 'sanctions_list']
    item_id: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,79}$')
    publisher: Literal['DEMO_VENDOR_NOT_REAL']
    issued_at: str
    files: list[FileEntry] = Field(min_length=1, max_length=32)
    model_format: Literal['MOCK_NPZ', 'TESSERACT_CONFIG'] | None = None
    runtime: str | None = None
    minimum_service_version: str | None = None
    list_source: Literal['SYNTHETIC_TEST', 'TRADEPROOF_JSON'] | None = None
    record_count: int | None = Field(default=None, ge=0, le=100000)
    test_only: bool | None = None

    @model_validator(mode='after')
    def profile(self):
        dt = datetime.fromisoformat(self.issued_at.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('Timezone required')
        paths = [f.path for f in self.files]
        if self.package_type == 'ocr_model':
            expected = {'payload/model_config.json'}
            if self.model_format == 'MOCK_NPZ':
                expected.add('payload/weights.npz')
        else:
            expected = {'payload/sanctions.json' if self.list_source == 'TRADEPROOF_JSON' else 'payload/sanctions.csv'}
        if len(paths) != len(set(paths)) or set(paths) != expected:
            raise ValueError('Unexpected payload paths')
        if self.package_type == 'ocr_model':
            if not all((self.model_format, self.runtime, self.minimum_service_version)):
                raise ValueError('Model metadata required')
        elif self.record_count is None or not self.list_source or (self.list_source == 'SYNTHETIC_TEST' and self.test_only is not True):
            raise ValueError('Synthetic list metadata required')
        return self


class ModelConfig(StrictModel):
    format: Literal['MOCK_NPZ']
    model_id: str
    runtime: str
    service_compatibility: list[str]
    weights_schema: dict[str, list[int]]
    note: str | None = None

