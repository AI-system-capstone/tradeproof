# 추출 대상 필드 정의 (초안)

> **필드 이름은 A의 `common/contracts.py`(거래 필드 13개)와 정확히 같아야 합니다.**
> 아래는 규약 예시에 나온 이름으로 채운 초안입니다. 데이터 담당과 A가 11/11까지 13개를 확정하세요.

| # | 필드 (contracts 이름) | 서류 | 예시 | 매칭 대상 | 비고 |
|---|---|---|---|---|---|
| 1 | `seller_name` | invoice | Falcon Oakwood Semiconductor LLC | O | |
| 2 | `buyer_name` | invoice | ASTERIA HARBOR TRADING | O | 시연 제재 대상(DEMO-A001)과 매칭 |
| 3 | `vessel_name` | bl | EMERALD TRIUMPH | O | |
| 4 | `amount` | invoice | 363,517.00 | X | |
| 5~13 | (확정 필요) | | | | 예: 통화, 송장 번호, 선적항, 도착항, shipper, consignee, notify party, 품목, 일자 |

## 매칭 대상 이름을 만드는 규칙 (유사도 매칭 시험용)

| 변형 | 예 | 기대 분류 (리스트에 원형이 있을 때) |
|---|---|---|
| 그대로 | ASTERIA HARBOR TRADING LLC | HIGH_RISK_HOLD |
| 법인 표기 생략·변경 | ASTERIA HARBOR TRADING | 규칙에 따름 (A의 매칭 임계값) |
| 철자 1~2자 변형 | ASTERlA HARBOR TRADlNG | 규칙에 따름 |
| 무관한 이름 | NORTHWIND CERAMICS | PASS_CANDIDATE |

기대 분류는 **A의 고정 규칙을 정답 필드값에 적용**해 계산합니다 (OCR 결과 아님).
