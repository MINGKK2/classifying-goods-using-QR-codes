# Hướng dẫn sử dụng QR Vision Studio

## 1. Chuẩn bị

### Phần mềm

- Python 3.10 trở lên.
- Các thư viện trong `requirements.txt`.
- Driver USB-UART đúng loại chip đang sử dụng.

### Phần cứng

- Camera USB.
- STM32F103 đã nạp firmware tương thích.
- Cáp UART mức logic 3,3 V.
- Cảm biến hồng ngoại.
- L298N và động cơ băng tải.
- Relay/driver van, van 5/2 và nguồn phù hợp.
- E-stop vật lý.

Trong lần thử đầu, hãy ngắt khí nén và thay đầu ra piston bằng LED.

## 2. Cài đặt

### Cách tự động trên Windows

Nhấp đúp `run_qr_scanner.bat`. Script tạo `.venv`, cài thư viện và mở ứng dụng.

### Cách thủ công

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe qr_scanner.py
```

## 3. Chuẩn bị cấu hình

### Route

Sửa `route_rules.json` trước khi chạy:

```json
{
  "__DEFAULT__": "HOLD",
  "QR_SAN_PHAM_A": "P1",
  "QR_SAN_PHAM_B": "P2",
  "QR_HANG_LOI": "REJECT"
}
```

Giữ route mặc định là `HOLD` trong quá trình thử.

### Chống quét lặp

`scanner_settings.json` mặc định:

```json
{
  "pass_release_seconds": 0.45,
  "stable_frames": 3
}
```

Khởi động lại ứng dụng sau khi sửa file này.

## 4. Mở camera

1. Chọn chỉ số camera.
2. Nhấn **Bắt đầu**.
3. Xác nhận có hình realtime và FPS lớn hơn 0.
4. Đưa QR vào vùng nhìn.
5. Kiểm tra polygon và nhãn QR xuất hiện đúng vị trí.

Nếu camera không mở:

- đóng Zoom, Teams, trình duyệt hoặc phần mềm khác đang giữ camera;
- thử camera index khác;
- rút và cắm lại camera;
- kiểm tra quyền Camera trong Windows Settings.

## 5. Kết nối STM32

1. Đóng Serial Monitor của Arduino IDE hoặc STM32CubeIDE.
2. Chọn đúng cổng COM.
3. Chọn baud `115200`.
4. Nhấn **Kết nối**.
5. Ứng dụng gửi `HELLO`.
6. Chờ UI hiển thị MCU sẵn sàng sau khi nhận `READY`.

Nếu COM đã mở nhưng chưa có `READY`, không nhấn START. Kiểm tra baud, TX/RX, mass chung và firmware.

## 6. Chạy dây chuyền

1. Đặt PWM thấp, ví dụ 20-30%.
2. Nhấn **Gửi PWM**.
3. Nhấn **START**.
4. Kiểm tra motor quay đúng chiều.
5. Đưa một sản phẩm qua camera.
6. Kiểm tra một dòng mới xuất hiện trong lịch sử.
7. Theo dõi trạng thái:
   - `SENT`: PC đã gửi `QUEUE`;
   - `ACK`: STM32 đã enqueue;
   - `ACTIVE`: cảm biến đã kích, STM32 đang xử lý route;
   - `DONE`: cơ cấu đã hoàn tất;
   - `ERROR`: UART hoặc STM32 báo lỗi.

## 7. Ý nghĩa khu vực giao diện

| Khu vực | Chức năng |
|---|---|
| Camera | Chọn camera, bật/tắt, xem FPS và polygon QR |
| UART | Chọn COM, baud, kết nối và ngắt kết nối |
| Dây chuyền | START, STOP và đặt PWM |
| Telemetry | Cảm biến, FIFO, tốc độ, trạng thái cơ cấu, heartbeat |
| Lịch sử | `event_id`, thời gian, QR, route và trạng thái MCU |

Nút dừng camera chỉ dừng việc thu hình. Nút `STOP` mới gửi lệnh dừng dây chuyền đến STM32.

## 8. Trình tự xử lý một sản phẩm

1. Camera đọc QR ổn định đủ số frame.
2. PassageGate tạo một lượt mới.
3. Ứng dụng tra route và ghi SQLite.
4. Ứng dụng gửi `QUEUE` nếu MCU đã `READY`.
5. STM32 phản hồi `ACK` và tăng FIFO.
6. Sản phẩm đến cảm biến tại cơ cấu.
7. STM32 lấy phần tử đầu FIFO, gửi `ACTIVE` và kích route.
8. Hết xung piston, STM32 gửi `DONE`.

## 9. Xuất lịch sử

Nhấn **Xuất CSV** và chọn vị trí lưu. File chứa toàn bộ bản ghi SQLite theo thứ tự `event_id`, kể cả bản ghi không còn nằm trong vùng lịch sử đang hiển thị.

Database cục bộ là `qr_events.db`. Không sửa database khi ứng dụng đang chạy.

## 10. Dừng hệ thống

Thứ tự khuyến nghị:

1. Nhấn **STOP**.
2. Xác nhận trạng thái `STOPPED` và PWM bằng 0.
3. Ngắt kết nối UART.
4. Dừng camera.
5. Thoát ứng dụng.
6. Cắt nguồn công suất và nguồn khí nén nếu không tiếp tục thử.

Ứng dụng cố gắng gửi `STOP` khi đóng, nhưng thao tác này không thay thế E-stop hoặc liên động phần cứng.

## 11. Checklist nghiệm thu chức năng

- [ ] Camera mở đúng và không bị méo tỷ lệ.
- [ ] Một QR đứng yên chỉ tạo một sự kiện.
- [ ] Cùng QR rời khung rồi quay lại tạo `event_id` mới.
- [ ] QR chưa cấu hình nhận route `HOLD`.
- [ ] STM32 phản hồi `READY`, `STATE`, `FIFO`, `SENSOR`, `HEARTBEAT`.
- [ ] `START`, `STOP`, `SPEED` hoạt động đúng.
- [ ] FIFO giữ đúng thứ tự khi quét nhiều sản phẩm.
- [ ] Cảm biến chỉ dequeue một phần tử ở mỗi cạnh kích hoạt.
- [ ] P1, P2 và REJECT kích đúng đầu ra.
- [ ] `DONE` xuất hiện sau khi xung cơ cấu kết thúc.
- [ ] CSV chứa đủ nội dung QR và trạng thái.
- [ ] E-stop vật lý cắt được cơ cấu công suất độc lập với PC.
