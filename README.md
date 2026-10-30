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

Mở trình duyệt trên máy chạy Docker, vào cổng **8000**, nhập URL trang video, nhấn **Lấy video**, chọn **Tải xuống**, rồi **Lưu file về máy**. Compose chỉ mở cổng trên loopback. Không đưa dịch vụ lên Internet: chưa có xác thực và chưa được gia cố để xử lý người dùng không tin cậy.

File được lưu trong volume `downloads`. Các phiên quét và trạng thái tải nằm trong RAM, mất khi khởi động lại; file đã tải vẫn còn trong volume. Xóa volume bằng `docker compose down -v` khi muốn xóa toàn bộ video. Không tự động dọn file. Mỗi file giới hạn dự kiến 2 GB, mỗi job tối đa 30 phút; giới hạn dung lượng không thay thế quota ổ đĩa.

Quét khoảng 10–45 giây. Tối đa hai tác vụ chạy cùng lúc, hàng đợi tải tối đa bốn tác vụ. Nếu playlist dùng token hết hạn, hãy quét lại. Cookie/Authorization không được chuyển sang trình tải; một số trang cần xác thực hoặc tương tác đặc biệt sẽ không hoạt động. Trình tải cần truy cập được cả domain playlist lẫn các domain chứa segment.

Không phải mọi trang video đều dùng HLS. Trang chỉ dùng DASH/MP4, nội dung DRM hoặc player không tự phát có thể không tìm thấy playlist. Việc kiểm tra DNS chặn URL nội bộ ở bước quét là biện pháp cơ bản, không phải cơ chế cách ly mạng hoàn chỉnh cho redirect, DNS rebinding hoặc URL segment do playlist cung cấp. Chỉ dùng với nguồn bạn tin cậy trong môi trường cá nhân.

## Tiến độ tải

Giao diện dùng một nút cho mỗi video: **Tải xuống → Hủy tải → Tải xuống** sau khi hủy hoặc hoàn tất. Khi xong, nhấn **Lưu file về máy** để lưu video. Bố cục theo kiểu MeTube: thanh đầu tối màu, thanh nhập URL liền nút bấm, hai danh sách **Có thể tải / Đã hoàn tất** dạng bảng với các cột tên, tiến độ, tốc độ, thời gian còn lại và nút thao tác. Nút ↓ là **Tải xuống**, chuyển thành × **Hủy tải** khi chạy. File xong tự chuyển sang **Đã hoàn tất**. Liên kết M3U8 hiện trực tiếp dưới ô tên video, tự xuống dòng khi dài và có thể bấm mở. Có nút đổi sáng/tối ở thanh đầu và bố cục riêng trên điện thoại.

Sau khi tìm được playlist, nhập **Tên video** rồi nhấn **Tải xuống**. Không cần nhập đuôi file: ứng dụng tự dùng đuôi của file thực tế, kể cả MP4 sau bước sửa đầu PNG. Để trống sẽ dùng `video`. Tên được áp dụng cho cả file trong volume (mỗi tác vụ có thư mục riêng) và file lưu về trình duyệt. Ký tự không hợp lệ được làm sạch; tên tối đa 200 ký tự đầu vào và được rút gọn để phù hợp giới hạn tên file.

Sau khi tải xong, ứng dụng đọc tối đa 2 MB đầu file để phát hiện chữ ký PNG và ít nhất 6 byte đồng bộ MPEG-TS cách nhau 188 byte. Nếu khớp, FFmpeg bỏ phần đầu PNG rồi đóng gói lại thành MP4 bằng `-c copy` (không mã hóa lại), bật `faststart` và kiểm tra bằng FFprobe: phải có video và thời lượng dương. Chỉ sau khi kiểm tra thành công mới thay file gốc và cho tải về. Nếu sửa lỗi thất bại, tác vụ báo lỗi và giữ file gốc; hủy tác vụ sẽ xóa file dở theo cơ chế hủy thông thường. File không có đầu PNG giữ nguyên. Đây là bước xử lý các file do ứng dụng tải mới; không tự quét thư mục MeTube `/mnt/metube`.

Nhấn **Hủy tải** để hủy tác vụ đang chờ, đang tải hoặc đang xử lý. Ứng dụng dừng tiến trình tải và các tiến trình con, xóa thư mục file tải dở rồi hiển thị **Đã hủy tải**. Có thể nhấn **Tải xuống** để tải lại từ đầu. Nút này không xóa video đã tải thành công. Các kết quả quét được giữ lại khi quét thêm URL để không mất nút hủy của tác vụ đang chạy.

Ứng dụng tải tối đa 8 đoạn HLS song song bằng yt-dlp (trước đây tải tuần tự). Có thể chỉnh biến môi trường `HLS_CONCURRENT_FRAGMENTS` từ 1 đến 32; thử 16 nếu nguồn cho phép, hoặc giảm xuống 2–4 nếu gặp HTTP 429/403. Tốc độ còn phụ thuộc máy chủ nguồn, đường truyền và ổ đĩa; tăng số đoạn không bảo đảm tăng tốc theo cùng tỷ lệ.

Mỗi video có thanh tiến độ, phần trăm, dung lượng đã tải/tổng dung lượng, tốc độ và thời gian còn lại; cập nhật khoảng mỗi giây. Đây là tiến độ tải về máy chủ Docker. Bước **Lưu file về máy** do trình duyệt quản lý riêng. Với HLS, tổng dung lượng và ETA có thể chỉ là ước tính (hiển thị `≈`); khi chưa có dữ liệu thì hiển thị `—` hoặc thanh chờ. Sau khi tải đủ dữ liệu, ứng dụng hiển thị trạng thái xử lý video và chỉ cung cấp file khi hoàn tất.

## Kiểm thử

Test tạo video màu xanh dài 4 giây gồm nhiều đoạn HLS bằng FFmpeg, phục vụ trang HTML có JavaScript phát sinh request M3U8, chạy Chromium thật để phát hiện rồi tải bằng yt-dlp thật và kiểm tra thời lượng bằng ffprobe. Máy chủ fixture tạo độ trễ cho segment và kiểm tra có nhiều request tải đồng thời. Chỉ trong test, bộ kiểm tra URL được thay để cho phép máy chủ fixture loopback.

```sh
docker build -t hls-web .
docker run --name hls-test --entrypoint sleep -d hls-web 300
docker cp tests hls-test:/tmp/tests
docker exec hls-test python -m unittest discover -s /tmp/tests -v
docker rm -f hls-test
```

Trong cloud có proxy CA riêng, build với `--secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt`. Không tắt kiểm tra TLS. Image không lưu CA của phiên cloud; khi chạy với proxy riêng cần cấp trust store phù hợp cho Chromium và yt-dlp.
