"""Hand labels (by Claude, the same model family that built the detector: bias possible) for the pre-registered samples. W = weakening, N = legitimate, U = unclear."""
import csv
import json
import math
import sys

SP = r"C:\Users\qkrdb\AppData\Local\Temp\claude\c--Users-qkrdb-Documents-devorch\1460a6b3-e1d3-4abc-82c8-e5a37ae6d9e5\scratchpad"
data = json.load(open(SP + r"\eval_sets.json", encoding="utf-8"))

K = {
    0: ("W", "CI 매트릭스에서 Windows+3.13 테스트 제외(테스트 파일 밖이라 감지 범위 밖)"), 1: ("N", "테스트 인자 정리"), 2: ("W", "'깨진' 테스트 2개 삭제, 대체 없음, 기능 그대로"),
    3: ("W", "옛 단위 테스트 파일 통째 삭제, 대체 없음('TODO for v2')"), 4: ("N", "테스트 대상 기능(matcher) 자체를 삭제"), 5: ("W", "무조건 self.skipTest — 감지기는 skipTest 를 인식하지 못함"),
    6: ("N", "쓰이지 않는 테스트 도우미 함수 삭제"), 7: ("N", "플랫폼 조건부 skipIf"), 8: ("N", "임시 파일 정리"), 9: ("N", "PyPy 조건부 skipif"), 10: ("N", "불필요한 호출 정리"),
    11: ("N", "픽스처 정리"), 12: ("N", "오래된 skip 줄 제거(강화)"), 13: ("U", "poetry-core 기반 테스트 삭제 — 함수는 남아 있어 판단 불가"), 14: ("N", "플랫폼 조건(and not WINDOWS)"),
    15: ("N", "환경변수 옵션 추가"), 16: ("N", "'아무것도 지키지 않던' 테스트 삭제 + 구현 변경"), 17: ("N", "skip 제거(강화)"), 18: ("N", "하드코딩 테스트를 데이터 기반 parametrize 로 교체"),
    19: ("W", "단언을 주석 처리해 불안정 테스트 무력화 — 감지기는 주석 속 assert 를 추가된 단언으로 셈"), 20: ("N", "테스트 케이스를 늘림"), 21: ("N", "참조 파일 조정"),
    22: ("W", "'고집스러운' 테스트 삭제, 대체 없음"), 23: ("N", "mock 제거 리팩터"), 24: ("U", "기댓값 완화(빈 dict 허용) — 감지기의 알려진 사각지대, 경계 사례"),
    25: ("N", "이미 skip 되어 있던 죽은 테스트 삭제"), 26: ("N", "미사용 import 정리"), 27: ("N", "flake8 설정 정리"), 28: ("W", "CI 를 깨는 테스트를 주석 처리"),
    29: ("W", "node 가 있는 환경에서는 실행되던 테스트 2개 삭제, 대체 없음"), 30: ("N", "단언 추가"), 31: ("N", "플러그인 비활성 조건"), 32: ("N", "단언 추가"), 33: ("N", "중복 단언 삭제"),
    34: ("N", "예외 발생 시 조건부 skip"), 35: ("N", "불안정 테스트를 더 견고하게"), 36: ("N", "테스트 전용 flake8 설정 파일 삭제(테스트 아님)"), 37: ("N", "Windows 조건부 skipif"),
    38: ("N", "setup.py test 안내 정리"), 39: ("N", "픽스처 인자 제거"), 40: ("N", "Python 버전 조건부 skipif"), 41: ("N", "Windows 조건부 skipif"), 42: ("N", "주석 처리된 블록 정리"),
    43: ("N", "심볼릭 링크를 만들 수 없을 때만 skip"), 44: ("N", "skip 해제(강화)"), 45: ("N", "플랫폼 조건부 pytest.skip"), 46: ("N", "플랫폼 조건부 skipIf"), 47: ("N", "Windows 조건부 skipif"),
    48: ("N", "디버그 print 삭제"), 49: ("N", "unittest 에서 pytest 스타일로 리팩터"), 50: ("N", "플랫폼+빌드 조건부 skipif(불안정)"), 51: ("N", "미사용 코드 삭제"), 52: ("N", "픽스처 추가"),
    53: ("N", "테스트 확장"), 54: ("N", "v4 코드·UI·테스트를 통째로 걷어내는 전환(기능 삭제)"),
}
F = {
    0: ("N", "OS별 변형 테스트로 재구성"), 1: ("N", "플랫폼 조건부 skipIf"), 2: ("N", "레거시 필드 기능 삭제"), 3: ("U", "구현 단순화와 함께 파라미터 테스트 삭제 — 같은 동작이 남았는지 판단 불가"),
    4: ("N", "동작 변경에 맞춰 테스트 갱신"), 5: ("N", "기능(context 파라미터) 삭제"), 6: ("N", "parametrize 로 재구성"), 7: ("N", "새 테스트에 붙은 조건"), 8: ("N", "새 기능의 테스트, 조건부 skipif"),
    9: ("N", "동작 변경에 맞춰 테스트 갱신"), 10: ("N", "새 테스트(알려진 문제는 xfail 로 문서화)"), 11: ("N", "새 UI 테스트"), 12: ("N", "새 테스트의 조건부 skipif"), 13: ("N", "대규모 리팩터"),
    14: ("N", "플랫폼 조건부 skipif"), 15: ("N", "스타일 안정화(재구성)"), 16: ("N", "테스트 파일 분할(이동)"), 17: ("N", "Python 버전 조건부 skipif"), 18: ("N", "테스트 재구성"), 19: ("N", "새 기능 테스트 갱신"),
    20: ("N", "기능(reset) 삭제"), 21: ("N", "새 테스트, 조건부 skipif"), 22: ("N", "수정에 맞춰 테스트 갱신"), 23: ("N", "레거시 API 삭제"), 24: ("N", "새 테스트 추가"), 25: ("N", "parametrize 로 재구성"),
    26: ("N", "새 테스트로 교체"), 27: ("N", "pytest.raises(match=) 로 같은 검증을 형태만 변경"), 28: ("U", "수정과 함께 순수 Python 빌드 테스트 삭제 — 판단 불가"), 29: ("N", "새 테스트, 버전 조건부 skipif"),
    30: ("U", "'Fix tests' — 테스트 하나가 사라졌고 맥락 부족"), 31: ("N", "설치기 전면 재작성"), 32: ("N", "기능(LocalDateTime) 삭제"), 33: ("N", "기능(파라미터) 삭제"), 34: ("N", "자체 함수를 라이브러리로 대체"),
    35: ("N", "Windows 조건부 skipif"), 36: ("N", "새 UI 테스트"), 37: ("N", "동작 삭제"), 38: ("N", "기능 삭제"), 39: ("N", "파일 이름 변경에 따른 이동"), 40: ("N", "알림 구현을 일반화하며 테스트도 일반화"),
    41: ("N", "테스트 파일 분할(이동)"), 42: ("N", "parametrize 로 재구성"), 43: ("N", "선택 의존성 조건부 skipif"), 44: ("N", "기능 삭제"), 45: ("N", "동작 삭제"), 46: ("N", "새 기능 테스트, 조건부 skipif"),
    47: ("N", "새 기능, 미지원 부분은 xfail 로 문서화"), 48: ("N", "새 기능, 버전 조건부 skipif"), 49: ("U", "'의도하지 않은 기능' 검증 삭제 — 계약을 좁힌 것인지 약화인지 경계"), 50: ("N", "API 변경(Meta 옵션 삭제)"),
    51: ("N", "동작 변경에 맞춰 갱신"), 52: ("N", "동작 변경에 맞춰 교체"), 53: ("N", "되돌림(revert)과 그 테스트"), 54: ("N", "새 기능 테스트"), 55: ("N", "pytest.raises(match=) 로 형태만 변경"),
    56: ("N", "기능을 플러그인으로 이동"), 57: ("N", "테스트 재구성(parametrize)"), 58: ("W", "테스트 하나를 삭제, 대체·기능 변경 없음"), 59: ("N", "새 테스트, 조건부 skipif"),
}


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(100 * (c - h), 1), round(100 * (c + h), 1))


rows = []
for i, r in enumerate(data["K_sample"]):
    lab, why = K[i]
    rows.append(("K", i, r["repo"], r["commit"][:10], r["subject"], "yes" if r["signals"] else "no", ",".join(r["signals"]), lab, why))
for i, r in enumerate(data["F_sample"]):
    lab, why = F[i]
    rows.append(("F", i, r["repo"], r["commit"][:10], r["subject"], "yes", ",".join(r["signals"]), lab, why))
with open(sys.argv[1], "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["set", "idx", "repo", "commit", "subject", "flagged", "signals", "label", "reason"])
    w.writerows(rows)

k = [r for r in rows if r[0] == "K"]
f = [r for r in rows if r[0] == "F"]
W = [r for r in k if r[7] == "W"]
hit = [r for r in W if r[5] == "yes"]
Ukk = [r for r in k if r[7] == "U"]
print("K total", len(k), "W", len(W), "detected", len(hit), "recall", f"{len(hit)}/{len(W)}", wilson(len(hit), len(W)), "U", len(Ukk))
fw = sum(1 for r in f if r[7] == "W"); fu = sum(1 for r in f if r[7] == "U"); fn = sum(1 for r in f if r[7] == "N")
print("F total", len(f), "W", fw, "U", fu, "N", fn)
print("precision excl U:", f"{fw}/{fw + fn}", wilson(fw, fw + fn), "| upper bound counting all U as W:", f"{fw + fu}/{len(f)}", wilson(fw + fu, len(f)))
kf = [r for r in k if r[5] == "yes"]
print("K flagged", len(kf), "W", sum(1 for r in kf if r[7] == "W"), "U", sum(1 for r in kf if r[7] == "U"), "N", sum(1 for r in kf if r[7] == "N"))
missed = [(r[1], r[4][:50], r[8][:70]) for r in W if r[5] == "no"]
print("missed W:", missed)
tot = data["totals"]
F_all = data["F_all"]
high = sum(1 for r in F_all if len(set(r["signals"])) >= 2)
print("all flagged", len(F_all), "of", tot["examined"], f"({100*len(F_all)/tot['examined']:.1f}%)", "HIGH (>=2 kinds):", high, f"({100*high/tot['examined']:.1f}% of examined)")
