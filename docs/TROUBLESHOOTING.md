# Xử lý sự cố QR Vision Studio

## 1. Ứng dụng không khởi động

### Hiện tượng

- Cửa sổ terminal đóng ngay.
- Báo không tìm thấy `python` hoặc `py`.
- Báo thiếu module.

### Kiểm tra

```powershell
py --version
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m py_compile qr_scanner.py
```

Nếu `.venv` bị hỏng, đóng ứng dụng, xóa riêng thư mục `.venv` rồi chạy lại `run_qr_scanner.bat`.

## 2. Camera không mở

Nguyên nhân thường gặp:

- camera đang bị ứng dụng khác sử dụng;
- chọn sai index;
- Windows chặn quyền camera;
- cáp USB hoặc nguồn camera không ổn định.

Cách xử lý:

1. Đóng Zoom, Teams, OBS và trình duyệt.
2. Thử camera index khác.
3. Kiểm tra **Settings > Privacy & security > Camera**.
4. Rút/cắm lại camera.
5. Thử camera bằng ứng dụng Camera của Windows.

Ứng dụng báo lỗi sau 20 lần liên tiếp không nhận được frame.

## 3. Có hình nhưng không đọc được QR

Kiểm tra:

- QR có đủ lớn trong khung không;
- camera có lấy nét tại mặt phẳng băng tải không;
- ảnh có bị phản sáng hoặc rung không;
- QR có bị che viền trắng hoặc biến dạng không;
- tốc độ băng tải có quá cao không.

Điều chỉnh theo thứ tự:

1. Tăng ánh sáng khuếch tán.
2. Giảm rung camera.
3. Tăng kích thước QR.
4. Giảm tốc băng tải.
5. Giảm `stable_frames` từ 3 xuống 2 để thử.
6. Chỉ giữ một QR trong vùng nhìn.

## 4. Một sản phẩm bị ghi nhiều lần

- Tăng `pass_release_seconds` nếu QR bị mất/hiện chập chờn.
- Tăng `stable_frames` nếu ảnh nhiễu tạo nhiều lượt ngắn.
- Cố định camera và ánh sáng.
- Kiểm tra QR có rời vùng nhìn hoàn toàn giữa hai kiện không.

Không giải quyết bằng cách retry hoặc lọc trùng vĩnh viễn theo nội dung, vì cùng một mã có thể xuất hiện trên nhiều sản phẩm khác nhau.

## 5. Hai sản phẩm cùng mã chỉ được ghi một lần

Giảm `pass_release_seconds` nếu khoảng trống giữa hai kiện ngắn. Camera phải có ít nhất một khoảng thời gian không nhìn thấy mã để PassageGate mở lượt mới.

Nếu không thể tạo khoảng trống hình ảnh, cần thêm cảm biến trigger tại vùng camera hoặc line-crossing.

## 6. Không thấy cổng COM

- Cài driver USB-UART.
- Kiểm tra Device Manager.
- Thử cáp USB có truyền dữ liệu.
- Rút/cắm lại thiết bị.
- Kiểm tra cổng COM có bị Arduino Serial Monitor chiếm không.

## 7. Mở COM được nhưng không có READY

Kiểm tra:

1. Baud phải là 115200.
2. TX/RX nối chéo.
3. Có mass chung.
4. Mức logic đúng 3,3 V.
5. Firmware đang dùng đúng đối tượng `Serial` cho cổng đã nối.
6. Reset MCU và quan sát terminal.
7. Gửi thủ công `HELLO` kèm LF.

Nếu firmware chạy trước khi PC mở COM, `HELLO` phải làm MCU phát lại `READY` và trạng thái.

## 8. Có READY nhưng QUEUE không vào FIFO

Theo dõi phản hồi:

- `ERR,<id>,BAD_QUEUE_FORMAT`: thiếu trường hoặc sai dấu phẩy.
- `ERR,0,BAD_EVENT_ID`: event ID không hợp lệ.
- `ERR,<id>,QUEUE_FULL`: FIFO đã đầy.
- `ERR,<id>,BAD_ROUTE`: route không hợp lệ nếu firmware có kiểm tra route.
- `LINE_TOO_LONG`: bản tin vượt bộ đệm UART.

Kiểm tra `MCU_PROTOCOL.md` và bảo đảm mỗi lệnh kết thúc bằng LF.

## 9. FIFO tăng nhưng cơ cấu không hoạt động

Kiểm tra theo thứ tự:

1. Trạng thái phải là `RUNNING`.
2. Cảm biến phải đổi sang active đúng cạnh.
3. UI có nhận `SENSOR,1` không.
4. MCU có gửi `ACTIVE,<id>,<route>` không.
5. Route có phải `HOLD` không.
6. GPIO có đổi mức khi đo bằng đồng hồ/logic analyzer không.
7. Module relay có đúng active HIGH/LOW không.
8. Nguồn van và liên động có được cấp không.

Thử bằng LED trước khi đấu van.

## 10. Cơ cấu kích sai thứ tự

Nguyên nhân có thể:

- nhiều QR nằm trong khung cùng lúc;
- sản phẩm vượt nhau giữa camera và cảm biến;
- cảm biến tạo nhiều cạnh;
- MCU reset làm mất FIFO;
- PC hoặc firmware tự retry `QUEUE` mà không chống trùng.

Khắc phục:

- thu hẹp vùng camera để chỉ có một sản phẩm;
- thêm máng dẫn hướng;
- tăng debounce cảm biến;
- kiểm tra `event_id` trong ACK/ACTIVE/DONE;
- không retry mù;
- bổ sung cơ chế đồng bộ FIFO sau reset.

## 11. Trạng thái UI không cập nhật

- Kiểm tra terminal có nhận dòng UART không.
- Bản tin phải đúng số trường và kết thúc LF.
- Không thêm khoảng trắng hoặc thông báo debug tùy ý vào cùng cổng UART.
- Dùng đúng tên lệnh: `STATE`, `SENSOR`, `FIFO`, `SPEED`, `ACK`, `ACTIVE`, `DONE`, `ERR`, `HEARTBEAT`.

## 12. Database hoặc CSV

### Database bị khóa

- Không mở `qr_events.db` bằng công cụ khác trong chế độ ghi khi ứng dụng chạy.
- Đóng các trình xem SQLite rồi thử lại.

### Muốn bắt đầu lịch sử mới

1. Đóng ứng dụng.
2. Sao lưu `qr_events.db` nếu cần.
3. Xóa riêng `qr_events.db`.
4. Mở lại ứng dụng để tạo database mới.

Không xóa database khi ứng dụng đang chạy.

## 13. Thu thập thông tin khi báo lỗi

Ghi lại:

- hệ điều hành và phiên bản Python;
- model camera và camera index;
- cổng COM, loại USB-UART và baud;
- commit Git đang chạy;
- nội dung `scanner_settings.json` và `route_rules.json`;
- 20-30 dòng terminal trước lỗi;
- bản tin UART cuối cùng;
- ảnh hoặc video hiện tượng;
- sơ đồ chân thực tế.

Không đăng công khai dữ liệu QR nhạy cảm hoặc thông tin sản xuất nếu repository ở chế độ public.
