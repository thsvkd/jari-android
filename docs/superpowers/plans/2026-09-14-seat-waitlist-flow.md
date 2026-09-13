# 실제 좌석 선택과 취소표 대기 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 실제 코레일 좌석표에서 즉시 예약 좌석 또는 취소표 대기 후보를 고르고, 떨어진 좌석은 한 자리씩 확보할 때마다 결제 알림을 보내며, 연속 좌석은 전원 좌석을 함께 확보한다.

**Architecture:** 코레일 원본 열차와 좌석 식별자는 서버의 짧은 수명 참조 저장소 안에 두고 앱에는 불투명 키와 화면용 좌석만 보낸다. 즉시 예약은 모바일 API 프로세스에서 좌석을 다시 확인한 뒤 지정 예약하고, 취소표 대기는 구조화한 좌석 계획을 기존 검색 프로세스에 전달해 좌석 현황과 후보를 교차 확인한다. 기존 조건과 실행 중 검색은 `seat_preference` 경로로 계속 읽는다.

**Tech Stack:** TypeScript, Vite, Vitest, Capacitor Android, Python 3.11, Flask, Redis, pytest, `korail-mobile-api` 1.1.1

**Spec:** `docs/superpowers/specs/2026-09-14-seat-waitlist-flow-design.md`

## Global Constraints

- 화면 문구는 `취소표 대기`와 `코레일 예약 대기`를 구분하고 `취소표 찾기`를 사용하지 않는다.
- 좌석표는 열차 종류를 하드코딩하지 않고 코레일 좌석 응답으로 그린다.
- 좌석 조회 실패를 빈자리 없음으로 표시하지 않는다.
- 좌석, 호차, 열차 참조 외의 코레일 원본 응답과 계정·세션 정보는 앱에 보내지 않는다.
- 떨어진 좌석은 한 자리 확보 때마다 저장하고 알린 뒤 결제를 기다리지 않고 남은 좌석을 계속 찾는다.
- 연속 좌석은 같은 열차·등급·호차의 전체 인원 조합만 한꺼번에 확보한다.
- 가족석은 실제 응답에서 판별 근거가 확인된 경우에만 표시한다.
- 실제 예약·취소와 서버 재시작 전에는 현재 검색·예약 상태를 확인한다.
- 비밀번호·토큰·Firebase 설정·서명키를 커밋하거나 검증 자료에 남기지 않는다.

실행 순서는 Task 1, 2, 3, 9, 4, 5, 6, 7, 8, 10으로 한다. 실제 좌석표의 지원 범위를 읽기 전용으로 먼저 확인한 뒤 API와 화면 계약을 확정한다.

---

### Task 1: 기준선과 즐겨찾기 계약

**Files:**
- Modify: `src/app.test.ts`
- Modify: `backend/tests/unit/test_mobile_booking.py`
- Modify only if a failing reproduction requires it: `src/app.ts`, `src/model.ts`, `backend/src/korail_bot/services/mini_app_gateway.py`

**Interfaces:**
- Consumes: `MobileApi.saveFavourite`, `MobileApi.deleteFavourite`, `conditionsToDraft`.
- Produces: 즐겨찾기가 날짜·열차 번호·좌석 후보를 복원하지 않는 회귀 테스트와 깨끗한 4.8.1 기준선.

- [x] **Step 1: 오래된 버전 기대값을 현재 패키지 버전으로 고친다**

`src/app.test.ts`의 설정 화면 기대값을 `v4.8.1`로 바꾼다. 제품 코드를 테스트에 맞춰 낮추지 않는다.

- [x] **Step 2: 즐겨찾기 전체 왕복 테스트를 실패하는 형태로 확장한다**

`test_real_gateway_delegates_booking_schedule_and_favourites`에서 저장 응답과 GET 응답의 `conditions`에 `dep_date`, `trains`, `seat_targets`가 없음을 확인하고, 프런트 테스트에서는 즐겨찾기를 누른 뒤 다음 날 날짜, 빈 열차 선택, 원래 구간·시간·등급이 복원되는지 확인한다.

```python
conditions = saved.json["favourites"][0]["conditions"]
assert conditions.get("dep_date", "") == ""
assert conditions.get("trains", []) == []
assert "seat_targets" not in conditions
assert http.get("/api/mobile/favourites", headers=headers).json["favourites"]
```

- [x] **Step 3: 테스트를 실행해 실제 결함을 구분한다**

Run: `npm test -- src/app.test.ts`와 `uv run --frozen pytest tests/unit/test_mobile_booking.py -q`

Expected: 버전 기대값 수정 후 통과한다. 즐겨찾기 왕복이 실패하면 그 실패가 가리키는 최소 코드만 수정한다.

- [x] **Step 4: 저장·불러오기·삭제 결과를 다시 검증한다**

Run: `npm test -- src/app.test.ts backend`가 아니라 프런트와 백엔드 명령을 각각 실행한다.

Expected: 프런트 대상 테스트와 `test_mobile_booking.py`가 모두 통과한다.

- [x] **Step 5: 커밋한다**

```powershell
git add src/app.test.ts src/app.ts src/model.ts backend/tests/unit/test_mobile_booking.py backend/src/korail_bot/services/mini_app_gateway.py
git commit -m "즐겨찾기 재사용 흐름을 검증한다"
```

### Task 2: 좌석 계획 도메인 모델

**Files:**
- Create: `backend/src/korail_bot/models/seat_plan.py`
- Modify: `backend/src/korail_bot/models/__init__.py`
- Modify: `backend/src/korail_bot/models/reservation.py`
- Modify: `backend/src/korail_bot/storage/redis.py`
- Create: `backend/tests/unit/test_seat_plan.py`

**Interfaces:**
- Consumes: 코레일 좌석의 `car_no`, 전송용 `seat_no`, 표시용 `specification`, `direction_code`.
- Produces: `SeatTarget`, `TrainSeatTargets`, `CancellationWaitPlan`, `TrainSearchParams.seat_plan_json`, `parse_seat_plan(text)`.

- [x] **Step 1: 좌석 계획 검증 테스트를 작성한다**

```python
def test_independent_plan_accepts_more_candidates_than_passengers():
    plan = CancellationWaitPlan.from_payload({
        "strategy": "independent", "passengerCount": 2,
        "trains": [{"trainNo": "015", "seatClass": "general", "targets": [
            {"carNo": 3, "seatNo": "S1", "label": "5A", "row": 5, "column": "A"},
            {"carNo": 3, "seatNo": "S2", "label": "5B", "row": 5, "column": "B"},
            {"carNo": 3, "seatNo": "S3", "label": "6A", "row": 6, "column": "A"},
        ]}],
    })
    assert plan.passenger_count == 2
    assert len(plan.trains[0].targets) == 3
```

연속 좌석 테스트는 같은 행에서 통로를 사이에 두지 않은 좌석만 묶고, 서로 다른 호차·등급·열차는 거부하도록 작성한다.

- [x] **Step 2: 테스트가 모델 부재로 실패하는지 확인한다**

Run: `uv run --frozen pytest tests/unit/test_seat_plan.py -q`

Expected: `CancellationWaitPlan` import 오류로 실패한다.

- [x] **Step 3: 불변 도메인 모델과 제한을 구현한다**

`SeatTarget`은 `car_no: int`, `seat_no: str`, `label: str`, `row: int | None`, `column: str`, `direction: str`, `adjacency_group: str`를 가진다. `CancellationWaitPlan`은 `strategy`, `passenger_count`, 열차별 후보를 검증하고 JSON 직렬화·역직렬화를 제공한다. 열차 30개, 열차당 후보 200개, 문자열 64자 제한을 둔다.

`TrainSearchParams`에 기본값이 빈 문자열인 `seat_plan_json`을 마지막 필드로 추가한다. Redis 직렬화는 필드를 저장하고 예전 레코드는 빈 문자열로 읽는다.

- [x] **Step 4: 모델과 Redis 호환 테스트를 통과시킨다**

Run: `uv run --frozen pytest tests/unit/test_seat_plan.py tests/unit/test_mobile_runtime.py -q`

Expected: 새 계획 왕복과 기존 검색 레코드 복원이 모두 통과한다.

- [x] **Step 5: 커밋한다**

```powershell
git add backend/src/korail_bot/models backend/src/korail_bot/storage/redis.py backend/tests/unit/test_seat_plan.py backend/tests/unit/test_mobile_runtime.py
git commit -m "취소표 대기 좌석 계획을 구조화한다"
```

### Task 3: 코레일 좌석 조회와 지정 예약 어댑터

**Files:**
- Modify: `backend/src/korail_bot/services/korail_service.py`
- Create: `backend/src/korail_bot/services/seat_map_service.py`
- Create: `backend/tests/unit/test_korail_seat_map.py`

**Interfaces:**
- Consumes: `KorailClient.get_seat_cars`, `get_seat_inventory`, `KorailSeatAssignment.from_inventory`, `KorailReservationJobType.SEAT_DESIGNATED`.
- Produces: `KorailService.search_selectable_trains(...)`, `seat_cars(train, seat_class, passenger_count)`, `seat_inventory(train, car_no, seat_class, passenger_count)`, `reserve_designated(train, inventory, targets, passenger_count)`와 `SeatMapService`.

- [ ] **Step 1: 코레일 호출 경계를 모킹한 실패 테스트를 작성한다**

```python
def test_reserve_designated_uses_wire_seat_number_not_label():
    target = SeatTarget(car_no=3, seat_no="000041", label="5A", row=5, column="A")
    hold = service.reserve_designated(train, inventory, [target], passenger_count=1)
    kwargs = service._modern_client.reserve.call_args.kwargs
    assert kwargs["job_type"] is KorailReservationJobType.SEAT_DESIGNATED
    assert kwargs["seats"][0].seat_no == "000041"
```

호차 등급 코드, 판매 불가 좌석 거부, 승객 수 불일치, 로그인 만료 전달도 테스트한다.

- [ ] **Step 2: 테스트가 새 메서드 부재로 실패하는지 확인한다**

Run: `uv run --frozen pytest tests/unit/test_korail_seat_map.py -q`

Expected: `search_selectable_trains` 또는 `reserve_designated` 부재로 실패한다.

- [ ] **Step 3: 어댑터와 화면용 변환을 구현한다**

`SeatMapService`는 사용자별 10분 TTL의 무작위 `trainKey`와 실제 열차 객체를 메모리에 보관한다. `get_train(owner_id, train_key)`는 소유자와 만료 시간을 검사한다. 좌석 응답에는 `carNo`, `label`, `salePossible`, `direction`, `floor`, `row`, `column`, `adjacencyGroup`, `familyLabel`만 포함한다.

`familyLabel`은 응답 속성 이름에 명시적으로 가족·유아동반 표지가 있을 때만 채운다. 방향만으로 가족석이라는 이름을 만들지 않는다.

- [ ] **Step 4: 어댑터 테스트를 통과시킨다**

Run: `uv run --frozen pytest tests/unit/test_korail_seat_map.py tests/unit/test_korail_waitlist.py -q`

Expected: 좌석 조회·지정 예약 테스트와 기존 코레일 예약 대기 테스트가 통과한다.

- [ ] **Step 5: 커밋한다**

```powershell
git add backend/src/korail_bot/services/korail_service.py backend/src/korail_bot/services/seat_map_service.py backend/tests/unit/test_korail_seat_map.py
git commit -m "실제 코레일 좌석표와 지정 예약을 연결한다"
```

### Task 4: 모바일 좌석 API와 즉시 예약

**Files:**
- Modify: `backend/src/korail_bot/mobile/runtime.py`
- Modify: `backend/src/korail_bot/mobile/gateway.py`
- Modify: `backend/src/korail_bot/mobile/api.py`
- Modify: `backend/src/korail_bot/services/mini_app_gateway.py`
- Modify: `backend/tests/unit/test_mobile_api.py`
- Modify: `backend/tests/unit/test_mobile_booking.py`

**Interfaces:**
- Consumes: Task 3의 `SeatMapService`와 `trainKey`.
- Produces: `GET /api/mobile/trains/<train_key>/cars`, `GET /api/mobile/trains/<train_key>/cars/<car_no>/seats`, `POST /api/mobile/reservations/designated`.

- [ ] **Step 1: 사용자 경계와 좌석 경쟁 테스트를 작성한다**

다른 사용자의 `trainKey`는 404, 만료 키는 410, 판매 완료 좌석은 409를 기대한다. 성공 응답은 다음 최소 계약을 확인한다.

```python
assert response.json == {
    "reserved": True,
    "pending": [{
        "reservationId": "R1", "trainInfo": "KTX 015 서울 → 부산",
        "expiresAt": expires.isoformat(), "seatNumber": "5A", "seatClass": "general",
    }],
    "paymentUrl": "https://www.letskorail.com/",
}
```

- [ ] **Step 2: 새 경로가 없어 실패하는지 확인한다**

Run: `uv run --frozen pytest tests/unit/test_mobile_api.py tests/unit/test_mobile_booking.py -q`

Expected: 좌석 API 경로가 404로 실패한다.

- [ ] **Step 3: 인증된 API와 예약 결과 저장을 구현한다**

모든 좌석 경로에 기존 `authenticated` 장식을 적용한다. 지정 예약 직전에 같은 호차 좌석표를 다시 읽고 대상이 모두 판매 가능한지 확인한다. 성공한 hold는 기존 `PaymentStatus` 또는 `MultiReservationStatus`에 먼저 저장한 뒤 모바일 알림을 생성한다. 요청의 예약 번호나 원본 코레일 필드는 신뢰하지 않는다.

- [ ] **Step 4: 모바일 API 테스트를 통과시킨다**

Run: `uv run --frozen pytest tests/unit/test_mobile_api.py tests/unit/test_mobile_booking.py -q`

Expected: 인증·소유권·경쟁·성공 저장 테스트가 통과한다.

- [ ] **Step 5: 커밋한다**

```powershell
git add backend/src/korail_bot/mobile backend/src/korail_bot/services/mini_app_gateway.py backend/tests/unit/test_mobile_api.py backend/tests/unit/test_mobile_booking.py
git commit -m "모바일 좌석 조회와 즉시 예약 API를 제공한다"
```

### Task 5: 앱 좌석 등급과 열차 행 동작

**Files:**
- Modify: `src/types.ts`
- Modify: `src/model.ts`
- Modify: `src/model.test.ts`
- Modify: `src/app.ts`
- Modify: `src/app.test.ts`
- Modify: `src/styles.css`

**Interfaces:**
- Consumes: 확장된 `TrainOption`의 `trainKey`, `generalAvailable`, `specialAvailable`, `waitlistEligible`.
- Produces: `seatClasses: Array<"general" | "special">`, `seatGradeMode: "any" | "specific"`, 열차별 `좌석 선택`, `취소표 대기`, `코레일 예약 대기` 동작.

- [ ] **Step 1: 화면 상태 전환 실패 테스트를 작성한다**

`좌석 등급 상관없음`이 기본으로 선택되고 세부 안내가 숨겨지는지, 해제하면 일반실·특실이 보이고 둘 다 해제한 제출은 막히는지 확인한다. 열차 상태별로 정확한 버튼 문구가 나타나며 `취소표 찾기`가 DOM에 없는지 확인한다.

- [ ] **Step 2: 테스트가 기존 네 개 라디오 UI 때문에 실패하는지 확인한다**

Run: `npm test -- src/model.test.ts src/app.test.ts`

Expected: 새 선택기와 열차별 버튼을 찾지 못해 실패한다.

- [ ] **Step 3: 모델 호환 변환과 UI를 구현한다**

새 앱 상태를 기존 `seat_option`으로 보낼 때 `any → 1`, `general → 2`, `special → 4`, `general+special → 1`로 변환한다. 마지막 경우에는 별도의 `seat_classes`를 함께 보내 의미를 보존한다. 구버전 조건은 기존 숫자에서 새 상태로 복원한다.

열차 행은 전체 행을 토글 버튼으로 쓰지 않고 등급 상태와 동작 버튼을 분리한다. 모든 터치 대상은 최소 44 CSS px로 만든다.

- [ ] **Step 4: 프런트 대상 테스트를 통과시킨다**

Run: `npm test -- src/model.test.ts src/app.test.ts`

Expected: 등급 선택, 호환 변환, 열차 행 문구 테스트가 통과한다.

- [ ] **Step 5: 커밋한다**

```powershell
git add src/types.ts src/model.ts src/model.test.ts src/app.ts src/app.test.ts src/styles.css
git commit -m "좌석 등급과 열차별 대기 동작을 다시 구성한다"
```

### Task 6: 실제 좌석표 화면과 즉시 예약 연결

**Files:**
- Create: `src/seat-map.ts`
- Create: `src/seat-map.test.ts`
- Modify: `src/api.ts`
- Modify: `src/api.test.ts`
- Modify: `src/types.ts`
- Modify: `src/app.ts`
- Modify: `src/app.test.ts`
- Modify: `src/styles.css`

**Interfaces:**
- Consumes: Task 4의 좌석 API.
- Produces: `groupSeatsByLayout(seats)`, `frontRowTargets`, `backRowTargets`, `consecutiveGroups`, 좌석표 모달과 즉시 예약 요청.

- [ ] **Step 1: 차량별 배치와 빠른 선택 테스트를 작성한다**

4열 일반실, 3열 특실, 행이 1로 시작하지 않는 차량, 층 정보가 있는 차량을 fixture로 넣는다. 맨 앞·맨 뒤는 실제 최소·최대 행을 사용하고, 통로를 가로질러 연속 좌석을 만들지 않는지 확인한다.

- [ ] **Step 2: 좌석표 모듈 부재로 실패하는지 확인한다**

Run: `npm test -- src/seat-map.test.ts src/api.test.ts src/app.test.ts`

Expected: `seat-map.ts` import 또는 좌석 화면 부재로 실패한다.

- [ ] **Step 3: 순수 좌석 배치 함수와 좌석 화면을 구현한다**

좌석표는 서버가 준 행·열·배치 그룹을 사용한다. 색상 외에도 아이콘·테두리·`aria-label`로 상태를 구분한다. 즉시 예약은 선택 수를 승객 수로 제한하고 확인 전에 열차, 등급, 호차, 좌석을 다시 보여준다.

- [ ] **Step 4: 좌석 경쟁 응답을 화면에서 처리한다**

409 응답이면 모달을 닫지 않고 좌석표를 다시 읽고 `선택한 좌석이 방금 판매됐어요. 좌석표를 새로 불러왔습니다.`를 표시한다. 성공하면 예약 화면으로 이동해 좌석, 기한, 결제 버튼을 보여준다.

- [ ] **Step 5: 프런트 대상 테스트를 통과시킨다**

Run: `npm test -- src/seat-map.test.ts src/api.test.ts src/app.test.ts`

Expected: 일반실·특실 배치, 앞뒤, 접근성, 경쟁 갱신, 예약 성공 화면이 통과한다.

- [ ] **Step 6: 커밋한다**

```powershell
git add src/seat-map.ts src/seat-map.test.ts src/api.ts src/api.test.ts src/types.ts src/app.ts src/app.test.ts src/styles.css
git commit -m "실제 좌석표에서 즉시 예약 좌석을 고른다"
```

### Task 7: 취소표 대기 검색과 부분 예약

**Files:**
- Modify: `backend/src/korail_bot/services/mini_app_service.py`
- Modify: `backend/src/korail_bot/services/mini_app_gateway.py`
- Modify: `backend/src/korail_bot/services/reservation_service.py`
- Modify: `backend/src/korail_bot/telegramBot/telebotBackProcess.py`
- Modify: `backend/src/korail_bot/mobile/worker.py`
- Modify: `backend/src/korail_bot/models/reservation.py`
- Modify: `backend/src/korail_bot/storage/base.py`
- Modify: `backend/src/korail_bot/storage/redis.py`
- Create: `backend/tests/unit/test_cancellation_wait.py`
- Modify: `backend/tests/unit/test_mobile_notifications.py`

**Interfaces:**
- Consumes: `TrainSearchParams.seat_plan_json`, Task 3의 지정 좌석 예약.
- Produces: 결제를 기다리지 않는 독립 좌석 검색, 전체 조합만 확보하는 연속 검색, `securedCount`, `targetCount`, 좌석별 결제 상태.

- [ ] **Step 1: 독립 좌석 부분 성공 테스트를 작성한다**

첫 조회에서 후보 한 자리만 판매 가능하고 다음 조회에서 두 번째 자리가 판매 가능하도록 rail fake를 만든다. `wait_for_payment`를 호출하지 않고 두 hold를 저장하며 `1/2`, `2/2` 알림이 순서대로 생성되는지 확인한다.

```python
assert storage.wait_for_payment.call_count == 0
assert [event["progress"] for event in events] == ["1/2", "2/2"]
assert process.finished
```

연속 좌석 테스트는 한 자리만 나온 첫 조회에서 예약 호출이 없고, 두 자리 조합이 나온 다음 조회에서 승객 2명의 지정 예약을 한 번 호출하는지 확인한다.

- [ ] **Step 2: 기존 결제 대기 동작 때문에 실패하는지 확인한다**

Run: `uv run --frozen pytest tests/unit/test_cancellation_wait.py -q`

Expected: 구조화 좌석 계획 미사용 또는 `wait_for_payment` 호출로 실패한다.

- [ ] **Step 3: 검색 프로세스에 좌석 계획을 전달한다**

`reservation_service.py`는 `seat_plan_json`을 마지막 argv로 전달하고 이전 인수 순서는 유지한다. 백그라운드 프로세스는 필드가 없으면 현재 `SeatPreference` 경로를 사용한다.

- [ ] **Step 4: 후보 좌석 검색을 구현한다**

독립 방식은 판매 가능한 후보 하나를 지정 예약하고 즉시 `MultiReservationStatus`에 저장한 뒤 status=2 콜백을 보낸다. 결제 대기는 제거한다. 남은 후보와 인원을 계속 조회한다.

연속 방식은 `CancellationWaitPlan.consecutive_groups()`가 만든 조합 중 전원이 판매 가능한 경우에만 한 번의 지정 예약을 호출한다. 일부 예약은 만들지 않는다.

- [ ] **Step 5: 만료와 중복 방지를 구현한다**

각 반복에서 기존 hold의 `ReservationOutcome`을 확인한다. 만료·해제된 좌석은 확보 수에서 제외해 다시 찾고, 유효하거나 결제된 예약은 제외한다. 예약 번호와 좌석 식별자를 Redis 집합에 기록해 같은 결과와 알림을 두 번 처리하지 않는다.

- [ ] **Step 6: 부분 예약과 알림 테스트를 통과시킨다**

Run: `uv run --frozen pytest tests/unit/test_cancellation_wait.py tests/unit/test_mobile_notifications.py tests/unit/test_mobile_booking.py -q`

Expected: 독립·연속·경쟁·만료·중복 시나리오가 통과한다.

- [ ] **Step 7: 커밋한다**

```powershell
git add backend/src/korail_bot/services backend/src/korail_bot/telegramBot/telebotBackProcess.py backend/src/korail_bot/mobile/worker.py backend/src/korail_bot/models/reservation.py backend/src/korail_bot/storage backend/tests/unit
git commit -m "취소표 대기에서 부분 좌석을 즉시 확보한다"
```

### Task 8: 취소표 대기 좌석 후보 UI와 상태

**Files:**
- Modify: `src/types.ts`
- Modify: `src/model.ts`
- Modify: `src/api.ts`
- Modify: `src/app.ts`
- Modify: `src/app.test.ts`
- Modify: `src/seat-map.ts`
- Modify: `src/seat-map.test.ts`
- Modify: `src/styles.css`
- Modify: `src/demo.ts`
- Modify: `src/demo.test.ts`

**Interfaces:**
- Consumes: Task 7의 `seatPlan`, `securedCount`, `targetCount`, 좌석별 pending 상태.
- Produces: 판매 완료 좌석을 후보로 고르는 취소표 대기 모드, 부분 예약 카드, 남은 대기 중지와 결제 버튼.

- [ ] **Step 1: 후보 선택과 부분 상태 실패 테스트를 작성한다**

승객 1명이 여러 후보를 선택할 수 있는지, 승객 3명 독립 방식에서 `1/3 예약 완료 · 2명 대기 중`과 결제 버튼이 표시되는지, 연속 방식은 가능한 전체 조합이 없으면 시작 버튼이 비활성화되는지 확인한다.

- [ ] **Step 2: 기존 좌석표가 판매 완료 좌석을 막아 실패하는지 확인한다**

Run: `npm test -- src/seat-map.test.ts src/app.test.ts src/demo.test.ts`

Expected: 취소표 대기 후보와 부분 상태 요소 부재로 실패한다.

- [ ] **Step 3: 취소표 대기 모드를 구현한다**

후보 모드에서는 판매 완료 좌석도 누를 수 있고 상태를 `예약 대상`으로 표시한다. 열차별 후보를 `seatPlan`에 저장한다. 여러 열차를 선택한 경우 각 열차가 유효한 후보를 가질 때만 마지막 확인으로 이동한다.

- [ ] **Step 4: 부분 예약과 결제 화면을 구현한다**

예약 카드에 열차, 등급, 좌석, 개별 기한을 표시한다. 한 자리 확보 때마다 새 앱 내 알림을 보이고 기존 카드에 누적한다. `남은 좌석 대기 중지`는 확보한 hold를 유지하고 검색만 멈춘다.

- [ ] **Step 5: 데모 데이터를 명확히 가짜로 유지한다**

데모 좌석표와 부분 예약 fixture를 추가하되 화면에 `체험 데이터` 표시를 유지한다. 데모 결과를 실서비스 성공 문구로 기록하지 않는다.

- [ ] **Step 6: 프런트 대상 테스트를 통과시킨다**

Run: `npm test -- src/seat-map.test.ts src/app.test.ts src/demo.test.ts`

Expected: 후보 선택, 연속 검증, 부분 상태, 결제 버튼이 통과한다.

- [ ] **Step 7: 커밋한다**

```powershell
git add src
git commit -m "취소표 대기 좌석 후보와 부분 예약을 보여준다"
```

### Task 9: 읽기 전용 실서비스 좌석표 검증

**Files:**
- Create: `backend/scripts/verify_mobile_seat_inventory.py`
- Create: `backend/tests/unit/test_seat_inventory_evidence.py`
- Create ignored evidence: `artifacts/seat-inventory-verification.json`

**Interfaces:**
- Consumes: 연결된 코레일 계정과 Task 3의 읽기 전용 좌석 조회.
- Produces: 민감정보 없는 차량·등급별 구조 검증 결과와 가족석 지원 여부.

- [ ] **Step 1: 증거 출력의 비밀정보 차단 테스트를 작성한다**

스크립트 결과에는 성공 여부, 차량 형식 범주, 호차 수, 좌석 수, 배치 유형, 등급, 방향·층·속성 필드 존재 여부만 허용한다. 역, 날짜, 열차 번호, 좌석 식별자, 계정, 쿠키, URL은 출력하지 않는지 검사한다.

- [ ] **Step 2: 스크립트를 구현하고 단위 테스트를 통과시킨다**

Run: `uv run --frozen pytest tests/unit/test_seat_inventory_evidence.py -q`

Expected: 허용 필드 검사와 마스킹 테스트가 통과한다.

- [ ] **Step 3: 현재 서버 상태를 바꾸지 않는 라이브 조회를 한 번 실행한다**

일반실·특실과 가능한 서로 다른 차량 형식을 조회한다. 매진 열차 좌석표가 반환되는지도 확인한다. 빠르게 반복하지 않고 각 경로의 요청 수를 제한한다.

- [ ] **Step 4: 지원 범위를 코드에 반영한다**

가족석 표지가 실제 응답에서 확인되면 명시 코드만 allowlist에 넣는다. 확인되지 않으면 가족석 버튼을 숨긴 채 이유를 검증 문서에 남긴다. 매진 열차 좌석표를 얻을 수 없다면 해당 열차에 실제 좌석 후보 선택을 제공하지 않고 조회 실패를 분명히 표시한다.

- [ ] **Step 5: 커밋한다**

```powershell
git add backend/scripts/verify_mobile_seat_inventory.py backend/tests/unit/test_seat_inventory_evidence.py backend/src
git commit -m "실제 좌석표 지원 범위를 검증한다"
```

### Task 10: 전체 회귀·Android·서버·실제 예약 검증

**Files:**
- Modify: `package.json`과 Android 버전 파일은 사용자에게 배포 버전을 확정받은 경우에만 변경한다.
- Create ignored evidence: `artifacts/seat-waitlist-verification.md`
- Create ignored evidence: `artifacts/device-*.png`

**Interfaces:**
- Consumes: Tasks 1-9의 최종 앱과 서버.
- Produces: 자동 테스트, 빌드, 실기기 화면, 서버 로그, 실제 지정 예약과 취소 결과를 연결한 검증 자료.

- [ ] **Step 1: 전체 자동 검증을 실행한다**

```powershell
npm test
npm run build
npm run build:demo
Set-Location backend
uv run --frozen pytest tests/unit -q
uv run --frozen ruff check src tests
Set-Location ..
powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1
```

Expected: 모든 명령이 종료 코드 0이고 테스트 실패가 없다.

- [ ] **Step 2: 서버 배포 전 활성 상태를 확인한다**

pi5-s2의 실행 중 검색 키, 예약 프로세스, 결제 전 예약을 확인한다. 사용자 데이터와 기존 코레일 예약 대기는 건드리지 않는다. 검색 또는 결제가 진행 중이면 재시작하지 않고 안전한 시점을 기다린다.

- [ ] **Step 3: 변경된 서버 파일만 배포하고 API를 재시작한다**

배포 후 서비스 active 상태, 헬스 응답, 외부 모바일 bootstrap, 로그의 예외 부재를 확인한다. 소스와 서버 커밋 또는 파일 해시를 기록한다.

- [ ] **Step 4: Android 실기기에 APK를 설치한다**

설치 후 세로 고정, 즐겨찾기 저장·불러오기·삭제, 일반실·특실 좌석표, 앞뒤 선택, 취소표 대기 후보, 부분 예약 카드, 결제 버튼, 알림 권한을 확인한다. 로그캣에서 AndroidRuntime·Capacitor 오류가 없는지 확인한다.

- [ ] **Step 5: 실제 지정 예약을 한 건 검증하고 바로 취소한다**

현재 판매 가능한 미래 열차 한 편과 한 좌석을 선택한다. 지정 예약 성공 후 앱에 실제 좌석, 결제 기한, 코레일 URL이 표시되고 푸시가 도착하는지 확인한다. 결제하지 않고 해당 테스트 hold만 취소한 뒤 코레일 미결제 목록에서 사라졌는지 확인한다.

- [ ] **Step 6: 취소표 대기의 비변경 경로를 검증한다**

매진 좌석 후보를 선택해 검색 요청이 서버에 저장되고 올바른 후보만 조회하는지 로그로 확인한다. 실제 빈자리가 나타나지 않으면 예약 성공을 주장하지 않는다. 장시간 테스트를 남겨 두지 않고 검증 검색만 중지한다.

- [ ] **Step 7: 증거 문서를 완성하고 최종 상태를 확인한다**

검증 문서에는 명령별 결과, 앱·서버 버전, APK SHA-256, 기기 모델, 좌석표 구조, 실제 지정 예약·취소 시각과 마스킹된 화면을 연결한다. `git status --short`, `git diff --check`, 브랜치 로그를 확인한다.

- [ ] **Step 8: 검증 문서를 제외한 제품 변경을 커밋한다**

```powershell
git add package.json package-lock.json android src backend scripts
git commit -m "실제 좌석 선택과 취소표 대기를 완성한다"
```

무단으로 원격 푸시, GitHub 릴리스 생성, 결제, 기존 예약 취소를 하지 않는다.
