# Giao thức UART PC ↔ STM32F103

Mặc định: `115200 baud`, 8 data bits, no parity, 1 stop bit. Mỗi bản tin là một dòng ASCII kết thúc bằng `\n`.

## PC gửi xuống MCU

```text
HELLO
START
STOP
SPEED,<0..100>
GET_STATUS
QUEUE,<event_id>,<route>,<qr_base64url>
```

- `HELLO`: yêu cầu MCU phát lại `READY` và trạng thái, giúp kết nối được cả khi MCU đã chạy trước lúc mở COM.
- `START`: chạy băng tải theo PWM đã đặt và cho phép xử lý cảm biến.
- `STOP`: cắt PWM, relay và toàn bộ đầu ra piston ngay lập tức. FIFO đang chờ được giữ nguyên.
- `SPEED`: đặt duty cycle L298N từ 0 đến 100%.
- `GET_STATUS`: yêu cầu MCU gửi lại toàn bộ trạng thái.
- `QUEUE`: thêm một kiện vào FIFO. Trường QR là UTF-8 mã hóa Base64 URL-safe, bỏ dấu `=`. PC gửi tối đa 72 byte nội dung vào MCU; SQLite vẫn lưu bản đầy đủ.

Ví dụ QR `SP-A,001`:

```text
QUEUE,101,P1,U1AtQSwwMDE
```

`event_id` là số nguyên tăng dần do SQLite cấp. `route` chỉ gồm chữ, số, `_` hoặc `-`.

## MCU gửi lên PC

```text
READY
STATE,RUNNING
STATE,STOPPED
SENSOR,0
SENSOR,1
FIFO,<count>,<capacity>
SPEED,<0..100>
ACK,<event_id>
ACTIVE,<event_id>,<route>
DONE,<event_id>
ACKCTRL,<command>
ERR,<event_id>,<reason>
HEARTBEAT,<uptime_ms>,<state>,<sensor>,<fifo_count>,<speed>
```

Luồng chuẩn của một kiện:

```text
PC  → QUEUE,101,P1,U1AtQSwwMDE
MCU → ACK,101
MCU → FIFO,1,32
... cảm biến phát hiện kiện ...
MCU → SENSOR,1
MCU → FIFO,0,32
MCU → ACTIVE,101,P1
MCU → DONE,101
```

## Quy tắc an toàn và FIFO

1. MCU khởi động ở trạng thái `STOPPED`; chỉ chạy motor sau `START`.
2. `STOP` luôn được chấp nhận sau khi cổng UART mở, kể cả trước `READY`.
3. MCU chống thêm trùng `event_id`; bản tin lặp được `ACK` nhưng không enqueue lần hai.
4. FIFO chỉ dequeue ở cạnh kích hoạt của cảm biến khi hệ thống đang `RUNNING`.
5. `HOLD` dequeue kiện nhưng không kích piston.
6. Timeout van/piston chạy độc lập trên MCU, không phụ thuộc PC.
7. Ứng dụng không retry mù khi thiếu `ACK`, tránh kích cơ cấu hai lần.
8. Nút dừng khẩn cấp vật lý và liên động phần cứng vẫn là bắt buộc; nút STOP trên UI không thay thế E-stop.
