# Giọng Việt

Ứng dụng desktop cá nhân chuyển văn bản thành tiếng nói, chạy trên Windows bằng VieNeu-TTS v3 Turbo. Giao diện và inference đều chạy cục bộ, không có server/API và không cần API key.

## Tính năng

- 25 giọng dựng sẵn của VieNeu-TTS, hỗ trợ tiếng Việt và tiếng Anh.
- Clone giọng từ clip WAV ngắn; giọng và clip mẫu được lưu trong thư mục dữ liệu riêng của ứng dụng để dùng lại ở lần sau.
- Tạo file MP3 192 kbps, nghe thử ngay trong ứng dụng và mở thư mục kết quả.
- Chạy VieNeu v3 Turbo bằng ONNX trên CPU, ưu tiên graph INT8 và tự dùng FP32 nếu graph INT8 chưa có trong cache. Số luồng CPU được tự cân theo máy (tối đa 12).

Chỉ clone giọng của bạn hoặc giọng bạn được phép sử dụng. Clip mẫu WAV sạch, khoảng 3–8 giây, thường cho kết quả tốt hơn.

## Chạy từ mã nguồn

Cần Python 3.11–3.13 và Windows 10/11 64-bit.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python main.py
```

Lần chạy đầu trên máy build cần internet để tải model VieNeu-TTS và các tệp phụ trợ. Sau khi cache đủ, `build.ps1` đóng gói các model này cùng ứng dụng. Inference chạy local; không gửi văn bản hay audio lên dịch vụ từ xa. Tốc độ đọc dùng mặc định của VieNeu, app không tự chỉnh tốc độ.

## Đóng gói

```powershell
.\build.ps1
```

Gói portable được tạo tại `dist/offline-build/GiongViet/`; ZIP64 đầy đủ tại `dist/GiongViet-offline-windows-x64.zip` (khoảng 4,4 GB). Chuyển ZIP sang máy Windows x64, giải nén toàn bộ rồi chạy `GiongViet/GiongViet.exe`. Không cần Python, cài thư viện hay internet để tải model trên máy nhận. Không chuyển riêng EXE: thư mục `_internal` chứa runtime và weights cần thiết.

## Dữ liệu

Với bản portable, giọng clone và file MP3 đầu ra được lưu trong `GiongViet/user-data/` cạnh ứng dụng để đi cùng thư mục khi sao chép. Khi tạo ZIP, các giọng clone đang có trong `%LOCALAPPDATA%\GiongViet\GiongViet\` trên máy build cũng được đưa vào gói. Bản chạy từ mã nguồn tiếp tục lưu dữ liệu trong `%LOCALAPPDATA%\GiongViet\GiongViet\`. Model và graph INT8 được cache theo cơ chế của Hugging Face; cache giọng clone tiếp tục được nạp lại khi mở ứng dụng. File MP3 đầu ra được lưu trong thư mục `outputs`.

## Nền tảng

- [VieNeu-TTS](https://github.com/pnnbao97/VieNeu-TTS) — Apache-2.0; cung cấp model và SDK local inference.
- [VieNeu-TTS trên Hugging Face](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo) — model v3 Turbo.
