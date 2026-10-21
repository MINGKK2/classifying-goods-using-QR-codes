# QR Vision Studio — Conveyor Sorter

Ứng dụng HMI phân loại hàng hóa bằng QR, camera OpenCV và STM32F103 điều khiển băng tải/piston qua UART hai chiều.

## Chức năng giao diện

- Camera realtime, vẽ khung QR và hiển thị FPS.
- Nút camera riêng và nút `START / STOP` dây chuyền riêng để vận hành an toàn.
- Chỉnh PWM băng tải 0–100% từ UI.
- Theo dõi trực tiếp cảm biến hồng ngoại, số phần tử FIFO, cơ cấu đang chạy và heartbeat MCU.
- Truyền cả `event_id`, tuyến phân loại và nội dung QR vào FIFO của STM32F103.
- Lưu toàn bộ lượt quét vào SQLite, xem lại và xuất CSV.

## Cơ chế đúng cho dây chuyền

Mỗi lần một QR xuất hiện ổn định trước camera được coi là một **lượt hàng**. QR phải biến mất khỏi camera ít nhất `0.45 giây` thì lần xuất hiện tiếp theo mới được coi là lượt mới.

Ví dụ cùng mã `ABC` đi qua ba lần:

```text
#101  ABC  P1
#102  ABC  P1
#103  ABC  P1
```

QR đứng yên trong camera vẫn chỉ tạo một sự kiện, không spam theo từng frame.

Có thể chỉnh trong `scanner_settings.json`:

```json
{
  "pass_release_seconds": 0.45,
  "stable_frames": 3
}
```

- Giảm `pass_release_seconds` nếu các kiện chạy nhanh và khoảng cách giữa hai kiện ngắn.
- Tăng `stable_frames` nếu camera hay đọc nhiễu; giảm nếu băng chuyền rất nhanh.

## Thứ tự và log

- SQLite tự cấp `event_id` tăng dần, không mất thứ tự khi khởi động lại ứng dụng.
- Log nằm tại `qr_events.db`.
- Mỗi bản ghi có thời gian, nội dung QR, tuyến phân loại và trạng thái MCU.
- Nút **Xuất CSV** xuất toàn bộ lịch sử trong SQLite, kể cả các dòng đã xóa khỏi màn hình.
- Terminal xuất JSON Lines, ví dụ:

```json
{"event_id": 101, "time": "2026-10-10T14:30:01.125", "qr": "ABC", "route": "P1", "mcu_status": "SENT"}
```

## Ánh xạ QR sang piston

Sửa `route_rules.json`:

```json
{
  "__DEFAULT__": "HOLD",
  "MA_HANG_A": "P1",
  "MA_HANG_B": "P2",
  "MA_HANG_LOI": "REJECT"
}
```

Ứng dụng tự nạp lại file sau khi bạn lưu. `HOLD` là tuyến an toàn mặc định: MCU không kích piston nhưng vẫn giữ vị trí kiện trong hàng đợi.

## Giao thức PC → MCU

Khi phát hiện một lượt hàng, PC gửi đúng một dòng. Nội dung QR được mã hóa Base64 URL-safe để dấu phẩy, xuống dòng và tiếng Việt không phá vỡ giao thức:

```text
QUEUE,<event_id>,<route>,<qr_base64url>\n
```

Ví dụ:

```text
QUEUE,101,P1,U1AtQSwwMDE
```

MCU phản hồi:

```text
READY
STATE,RUNNING
SENSOR,1
FIFO,3,32
ACK,101
ACTIVE,101,P1
DONE,101
ERR,101,NO_ROUTE
```

- `READY`: MCU đã khởi động xong; PC mới bắt đầu gửi FIFO.
- `ACK`: MCU đã thêm kiện vào FIFO.
- `DONE`: cảm biến cuối băng chuyền đã kích và piston xử lý xong.
- `ERR`: MCU từ chối hoặc gặp lỗi.

Ứng dụng không tự gửi lại khi thiếu ACK vì retry mù có thể làm piston kích hai lần. Nếu cần retry, MCU phải chống trùng bằng `event_id`.

## Vì sao MCU cần FIFO và cảm biến?

Camera chỉ xác định loại hàng và thứ tự. MCU nên lưu FIFO các lệnh `QUEUE`. Khi cảm biến quang ở cụm piston phát hiện kiện tới, MCU lấy phần tử đầu hàng đợi và kích piston tương ứng. Không nên dùng độ trễ từ PC làm thời điểm kích piston vì tốc độ băng chuyền và độ trễ camera có thể thay đổi.

Để thứ tự tuyệt đối, vùng nhìn của camera nên chỉ chứa một QR tại một thời điểm. Nếu nhiều kiện có thể chồng nhau trong cùng khung hình, cần thêm cảm biến trigger ở camera hoặc vùng ROI/line-crossing theo hướng băng chuyền.

## Chạy ứng dụng

Nhấp đúp:

```text
run_qr_scanner.bat
```

Trong giao diện:

1. Nạp `mcu_fifo_example.ino` cho STM32F103 sau khi sửa pin đúng với mạch thật.
2. Chọn camera và nhấn **Bắt đầu** để bật camera realtime.
3. Chọn cổng COM, baud `115200`, nhấn **Kết nối** và chờ `MCU sẵn sàng`.
4. Chọn PWM, nhấn **Gửi PWM**, rồi nhấn **START** để chạy dây chuyền.
5. Theo dõi cảm biến, FIFO, cơ cấu và cột trạng thái MCU trong nhật ký.
6. Nhấn **STOP** trước khi ngắt UART hoặc thoát ứng dụng. Ứng dụng cũng tự gửi STOP khi đóng.

## Lưu ý an toàn

- Mặc định dùng `HOLD`, không tự kích piston cho QR chưa cấu hình.
- Thử với đèn LED hoặc ngắt khí nén trước khi đấu van điện từ.
- MCU cần nút dừng khẩn cấp, timeout van và liên động phần cứng.
- Không tự động retry lệnh piston nếu chưa có cơ chế chống trùng `event_id` phía MCU.
- Nút STOP trên giao diện là lệnh phần mềm, không thay thế nút E-stop vật lý.

Chi tiết toàn bộ bản tin UART nằm trong `MCU_PROTOCOL.md`.
