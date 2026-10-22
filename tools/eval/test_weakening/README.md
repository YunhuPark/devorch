# 약해진 테스트 감지 — 평가 스크립트 (`docs/TEST_WEAKENING_EVAL.md`)

순서: ① 공개 저장소 6곳을 복제(`black`, `click`, `marshmallow`, `loguru`, `poetry`, `glances`) → ② `mine_eval.py` 로 사전 등록한 규칙대로 K/F 집합을 만든다 →
③ `show_eval.py K|F <시작> <끝>` 으로 라벨링용 요약을 본다 → ④ `labels.py` 가 라벨(`docs/test_weakening_labels.csv`)을 쓰고 재현율·정밀도·95% Wilson 구간을 계산한다.

스크립트의 경로 상수(`BASE`, `SP`)는 이 PC 기준이라 **자기 환경에 맞게 고쳐서** 쓴다. 복제한 코드는 *읽기만* 한다(실행하지 않음).
라벨은 Claude 가 붙였다(개발에 쓴 모델과 같은 계열이라 편향 가능) — 그래서 모든 라벨과 이유를 CSV 로 공개한다. 다른 사람이 다시 판정해 비교할 수 있다.
