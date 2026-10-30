# Web tải video HLS

Ứng dụng FastAPI mở trang bằng Chromium/Playwright, theo dõi response M3U8 rồi dùng yt-dlp và FFmpeg tải video. Dành cho video bạn sở hữu hoặc được phép tải; không vượt DRM, đăng nhập hay CAPTCHA.

## Chạy bằng Docker

```sh
docker compose up --build -d
```

## Tự build và đẩy lên Docker Hub bằng GitHub Actions

Workflow nằm tại `.github/workflows/docker.yml`. Trong repository GitHub, vào **Settings → Secrets and variables → Actions → New repository secret**, thêm:

- `DOCKERHUB_USERNAME`: username Docker Hub của bạn.
- `DOCKERHUB_TOKEN`: access token Docker Hub có quyền ghi/push vào repository đích. Không dùng mật khẩu tài khoản và không đưa token vào mã nguồn.

Tạo repository `jav-downloader` trong tài khoản Docker Hub đó. Nếu muốn tên khác, thêm Actions **Variable** `DOCKERHUB_REPOSITORY`, ví dụ `hls-web` (chỉ tên repository, không kèm username).

Đẩy mã nguồn và workflow lên nhánh `main`. Mỗi lần push `main`, workflow sẽ build image Linux AMD64, chạy kiểm thử Chromium + tải video mẫu + giao diện rồi mới đăng nhập và push. Pull request vào `main` chỉ build/test, không dùng Docker Hub secrets hoặc push image. Có thể chạy thủ công ở **Actions → Build, test and publish Docker image → Run workflow**.

Image có tên `USERNAME/jav-downloader:latest` cho nhánh mặc định và tag `sha-<commit SHA đầy đủ>`. Push Git tag dạng `v1.0.0` sẽ tạo thêm Docker tag `v1.0.0`. Hiện chỉ build AMD64; chưa hỗ trợ máy ARM như Raspberry Pi.

Sau khi workflow thành công, trên máy có Docker chạy (thay `USERNAME`):

```sh
docker run -d --name hls-web --init --restart unless-stopped \
  --shm-size=1g \
  -p 127.0.0.1:8000:8000 \
  -v hls-downloads:/downloads \
  USERNAME/jav-downloader:latest
```

Docker Hub lưu image; web chạy trên máy thực thi lệnh Docker. Nếu repository Docker Hub là private, cần `docker login` trước khi pull/chạy.

Mở trình duyệt trên máy chạy Docker, vào cổng **8000**, nhập URL trang video, nhấn **Tìm M3U8**, chọn **Tải video**, rồi **Lưu file về máy**. Compose chỉ mở cổng trên loopback. Không đưa dịch vụ lên Internet: chưa có xác thực và chưa được gia cố để xử lý người dùng không tin cậy.

File được lưu trong volume `downloads`. Các phiên quét và trạng thái tải nằm trong RAM, mất khi khởi động lại; file đã tải vẫn còn trong volume. Xóa volume bằng `docker compose down -v` khi muốn xóa toàn bộ video. Không tự động dọn file. Mỗi file giới hạn dự kiến 2 GB, mỗi job tối đa 30 phút; giới hạn dung lượng không thay thế quota ổ đĩa.

Quét khoảng 10–45 giây. Tối đa hai tác vụ chạy cùng lúc, hàng đợi tải tối đa bốn tác vụ. Nếu playlist dùng token hết hạn, hãy quét lại. Cookie/Authorization không được chuyển sang trình tải; một số trang cần xác thực hoặc tương tác đặc biệt sẽ không hoạt động. Trình tải cần truy cập được cả domain playlist lẫn các domain chứa segment.

Không phải mọi trang video đều dùng HLS. Trang chỉ dùng DASH/MP4, nội dung DRM hoặc player không tự phát có thể không tìm thấy playlist. Việc kiểm tra DNS chặn URL nội bộ ở bước quét là biện pháp cơ bản, không phải cơ chế cách ly mạng hoàn chỉnh cho redirect, DNS rebinding hoặc URL segment do playlist cung cấp. Chỉ dùng với nguồn bạn tin cậy trong môi trường cá nhân.

## Kiểm thử

Test tạo video màu xanh dài 2 giây bằng FFmpeg, phục vụ trang HTML có JavaScript phát sinh request M3U8, chạy Chromium thật để phát hiện rồi tải bằng yt-dlp thật và kiểm tra thời lượng bằng ffprobe. Chỉ trong test, bộ kiểm tra URL được thay để cho phép máy chủ fixture loopback.

```sh
docker build -t hls-web .
docker run --name hls-test --entrypoint sleep -d hls-web 300
docker cp tests hls-test:/tmp/tests
docker exec hls-test python -m unittest discover -s /tmp/tests -v
docker rm -f hls-test
```

Trong cloud có proxy CA riêng, build với `--secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt`. Không tắt kiểm tra TLS. Image không lưu CA của phiên cloud; khi chạy với proxy riêng cần cấp trust store phù hợp cho Chromium và yt-dlp.
