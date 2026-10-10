"""Service configuration, read once from the environment (see the contract's env-var table)."""
import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    checkpoint: str = "convaiinnovations/laya"   # LAYA_CHECKPOINT: HF repo id or a local checkpoint dir
    checkpoint_subdir: Optional[str] = None      # LAYA_CHECKPOINT_SUBDIR: folder inside the HF repo (only it is fetched)
    fallback_checkpoint: Optional[str] = "convaiinnovations/laya"   # LAYA_FALLBACK_CHECKPOINT: "" = no fallback
    device: str = "auto"                         # LAYA_DEVICE: auto | cpu | cuda | cuda:N
    amp: bool = True                             # SPOT_AMP: 1 = fp16 autocast on GPU (ignored on CPU), 0 = fp32
    port: int = 8082                             # PORT
    host: str = "0.0.0.0"                        # HOST
    logs_database_url: Optional[str] = None      # LOGS_DATABASE_URL: log calls to app.model_calls when set
    max_items: int = 64                          # events per /v1/predict call (contract)
    max_batch: int = 0                           # LAYA_MAX_BATCH: (event, question) pairs per forward pass;
                                                 #   0 = auto: 256 on GPU (one pass for any request), 16 on CPU
    warmup: bool = True                          # LAYA_WARMUP: run one tiny prediction at startup

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            checkpoint=env("LAYA_CHECKPOINT") or cls.checkpoint,
            checkpoint_subdir=(env("LAYA_CHECKPOINT_SUBDIR") or "").strip("/ ") or None,
            fallback_checkpoint=(env("LAYA_FALLBACK_CHECKPOINT", cls.fallback_checkpoint) or "").strip() or None,
            device=env("LAYA_DEVICE") or cls.device,
            amp=env("SPOT_AMP", "1") != "0",
            port=int(env("PORT") or cls.port),
            host=env("HOST") or cls.host,
            logs_database_url=env("LOGS_DATABASE_URL") or None,
            max_batch=int(env("LAYA_MAX_BATCH") or 0),
            warmup=env("LAYA_WARMUP", "1") != "0",
        )
