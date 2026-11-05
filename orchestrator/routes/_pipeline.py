"""공유 Pipeline 싱글톤.

라우트 모듈들이 동일한 Pipeline 인스턴스를 공유하기 위한 모듈.
import 시점에 한 번만 인스턴스화된다.
"""

from __future__ import annotations

from pipelines.dual_model_pipeline import Pipeline

pipeline: Pipeline = Pipeline()
