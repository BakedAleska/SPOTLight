"""SYSTEM2_DEADLINE_S: the text-request deadline (default 15 s, the p95 target) is raisable for slow local models."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyst.live import assessor_from_env  # noqa: E402

BASE = {'SYSTEM2_LLAMA_URL': 'http://gateway:4000', 'SYSTEM2_MODEL': 'system2', 'SYSTEM2_CACHE_DIR': ''}


@pytest.mark.parametrize('raw,expect', [(None, 15.0), ('', 15.0), ('45', 45.0), ('7.5', 7.5)])
def test_deadline_env(raw, expect):
    env = dict(BASE)
    if raw is not None:
        env['SYSTEM2_DEADLINE_S'] = raw
    a = assessor_from_env(env)
    assert a.config.deadline_s == expect and a.generator.timeout_s == expect
    assert a.image_config.deadline_s == 25.0   # the image deadline is independent


@pytest.mark.parametrize('raw', ['soon', '0', '-3', '500', 'nan'])
def test_bad_deadline_env_is_refused(raw):
    with pytest.raises(RuntimeError, match='SYSTEM2_DEADLINE_S'):
        assessor_from_env(dict(BASE, SYSTEM2_DEADLINE_S=raw))
