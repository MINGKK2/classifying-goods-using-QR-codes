# Tích hợp phần cứng và STM32F103

## 1. Phạm vi firmware mẫu

`mcu_fifo_example.ino` là firmware tham khảo cho STM32F103 dùng Arduino STM32 core. Firmware thực hiện:

- UART 115200 baud;
- FIFO 32 phần tử;
- chống trùng `event_id` gần nhất;
- PWM băng tải;
- cảm biến active LOW có debounce;
- đầu ra `P1`, `P2`, `REJECT` và relay cấp phép;
- xung piston có timeout độc lập;
- heartbeat mỗi giây.

Firmware không phải cấu hình chân cuối cùng cho mọi bo hoặc PCB.

## 2. Sơ đồ chân gợi ý Blue Pill

| Chức năng | Chân mẫu | Ghi chú |
|---|---|---|
| Cảm biến | `PA1` | Active LOW, dùng `INPUT_PULLUP` |
| PWM L298N | `PA0` | Phải là chân hỗ trợ PWM |
| L298N IN1 | `PB0` | Chọn chiều động cơ |
| L298N IN2 | `PB1` | Chọn chiều động cơ |
| Relay cấp phép | `PB12` | Điều khiển qua tầng driver |
| Route P1 | `PB13` | Điều khiển qua driver/relay |
| Route P2 | `PB14` | Điều khiển qua driver/relay |
| Route REJECT | `PB15` | Điều khiển qua driver/relay |

Trước khi nạp firmware:

1. Đối chiếu pinout chính xác của bo STM32F103 đang dùng.
2. Kiểm tra chân PWM thuộc timer phù hợp.
3. Kiểm tra chân có bị dùng cho SWD/JTAG, USB hoặc ngoại vi khác không.
4. Kiểm tra mức kích relay là active HIGH hay active LOW.
5. Sửa hằng số chân và hàm đặt đầu ra nếu phần cứng khác giả định.

## 3. UART

### Kết nối tối thiểu

| USB-UART | STM32 |
|---|---|
| TX | RX của UART được chọn |
| RX | TX của UART được chọn |
| GND | GND |

- TX và RX phải nối chéo.
- Dùng mức logic 3,3 V; không đưa tín hiệu 5 V vào chân không chịu 5 V.
- Không cấp đồng thời nhiều nguồn cho bo nếu chưa xác định đường nguồn và diode bảo vệ.

Firmware mẫu dùng đối tượng `Serial`. Tùy Arduino core và cấu hình bo, `Serial` có thể là USB CDC hoặc một UART phần cứng. Hãy xác nhận cổng thực tế bằng một chương trình gửi `READY` đơn giản trước khi tích hợp toàn bộ firmware.

## 4. L298N và động cơ

- `ENA` nhận PWM từ `MOTOR_PWM_PIN`.
- `IN1` và `IN2` đặt chiều quay.
- Nguồn motor đi vào L298N, không lấy từ chân 5 V hoặc 3,3 V của STM32.
- Mass logic của STM32 và L298N cần tham chiếu chung nếu không dùng cách ly.
- Bảo đảm dòng khởi động của động cơ nằm trong giới hạn driver và nguồn.

Kiểm tra đầu tiên ở PWM 20-30%, không gắn tải cơ khí nặng.

## 5. Cảm biến hồng ngoại

Firmware mẫu giả định:

- đầu ra cảm biến active LOW;
- `INPUT_PULLUP`;
- debounce 60 ms;
- dequeue khi phát hiện cạnh kích hoạt trong trạng thái `RUNNING`.

Nếu cảm biến active HIGH, đảo logic trong firmware. Nếu tín hiệu cảm biến là 5 V, phải dùng chia áp, level shifter hoặc module ngõ ra phù hợp với STM32.

## 6. Relay và van điện từ

Không nối cuộn relay hoặc van trực tiếp vào GPIO.

Tầng công suất tối thiểu cần có:

- transistor/MOSFET hoặc module relay phù hợp;
- điện trở gate/base;
- diode flyback cho tải DC cảm;
- nguồn riêng đủ dòng cho van;
- cầu chì hoặc bảo vệ quá dòng;
- E-stop và liên động cắt nguồn van.

`LINE_RELAY_PIN` là đầu ra cấp phép chung. `P1_PIN`, `P2_PIN`, `REJECT_PIN` chọn route. Xung mặc định dài 250 ms và được tắt bằng timer trong vòng lặp, không chờ lệnh từ PC.

## 7. FIFO

Mỗi phần tử gồm:

```cpp
struct Job {
  uint32_t eventId;
  char route[16];
  char qrBase64[97];
};
```

- Dung lượng mặc định: 32.
- QR chứa tối đa 72 byte UTF-8 trước khi mã hóa.
- `event_id` trùng trong danh sách gần nhất được ACK nhưng không enqueue lại.
- FIFO đầy trả `ERR,<id>,QUEUE_FULL`.
- Cảm biến kích khi FIFO rỗng trả `ERR,0,SENSOR_WITH_EMPTY_QUEUE`.

## 8. Nạp và kiểm thử firmware

Quy trình khuyến nghị:

1. Chọn đúng board variant và upload method.
2. Nạp firmware khi chưa đấu tải công suất.
3. Mở Serial Monitor ở 115200 baud.
4. Reset MCU và kiểm tra `READY`.
5. Gửi `HELLO`, chờ `READY`, `STATE`, `SENSOR`, `FIFO`, `SPEED`.
6. Gửi `START`, `SPEED,30`, `STOP` và đo đầu ra PWM.
7. Gửi một lệnh mẫu:

   ```text
   QUEUE,1,P1,VEVTVA
   ```

8. Xác nhận `ACK,1` và `FIFO,1,32`.
9. Kích cảm biến bằng tay, xác nhận `ACTIVE,1,P1` rồi `DONE,1`.
10. Thử lại cùng `event_id` và xác nhận FIFO không tăng.
11. Thử `HOLD`, `P2`, `REJECT` bằng LED.
12. Chỉ đấu relay/van sau khi kết quả đúng.

## 9. Trạng thái an toàn

Sau reset hoặc `STOP`:

- PWM bằng 0;
- relay cấp phép tắt;
- P1, P2 và REJECT tắt;
- FIFO được giữ khi STOP nhưng không dequeue cho đến START;
- mọi xung cơ cấu đang hoạt động phải dừng.

Thiết kế thực tế phải bảo đảm trạng thái an toàn bằng phần cứng ngay cả khi firmware treo hoặc dây UART bị ngắt.

## 10. Tham số firmware cần hiệu chỉnh

| Hằng số | Mặc định | Ý nghĩa |
|---|---:|---|
| `PISTON_PULSE_MS` | 250 ms | Thời gian kích piston |
| `HEARTBEAT_MS` | 1000 ms | Chu kỳ telemetry tổng hợp |
| `SENSOR_DEBOUNCE_MS` | 60 ms | Lọc dội cảm biến |
| `QUEUE_CAPACITY` | 32 | Dung lượng FIFO |
| `DEFAULT_SPEED_PERCENT` | 60% | PWM sau khởi động, chỉ áp dụng khi START |

Điều chỉnh từng tham số trên mô hình thật và ghi lại kết quả thử nghiệm.
