# Phát hành bản cập nhật

Ứng dụng kiểm tra GitHub Releases của `ktiu6861-ui/VieNeu-TTS`. Nút cập nhật tải gói phần mềm, xác minh SHA-256, rồi thay ứng dụng portable tại chỗ. Thư mục model và toàn bộ `user-data` được giữ lại. Cần có kết nối internet khi kiểm tra và tải bản mới.

Khi phát hành phiên bản:

1. Tăng phiên bản trong `pyproject.toml` và `updater.py` cho khớp nhau, ví dụ `0.1.1`.
2. Chạy `build.ps1`. Bản portable đầy đủ và gói cập nhật sẽ được tạo trong `dist/`.
3. Tạo GitHub Release với tag `v0.1.1` (có `v` ở đầu).
4. Đính kèm hai tệp `GiongViet-update-windows-x64.zip` và `GiongViet-update-manifest.json` từ `dist/` vào release rồi xuất bản.

Người dùng đang có bản portable chỉ cần bấm **Cập nhật phiên bản mới**. ZIP portable đầy đủ vẫn dùng cho lần cài mới; gói cập nhật nhỏ hơn không chứa model hoặc dữ liệu cá nhân.
