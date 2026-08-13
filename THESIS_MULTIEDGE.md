HỌC VIỆN KỸ THUẬT QUÂN SỰ CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM
VIỆN CNTT & TT ĐỘC LẬP - TỰ DO - HẠNH PHÚC
BỘ MÔN AN TOÀN THÔNG TIN VÀ CÔNG NGHỆ MẠNG – VIỆN CÔNG NGHỆ
THÔNG TIN VÀ TRUYỀN THÔNG – HỌC VIỆN KỸ THUẬT QUÂN SỰ

-------------------------------------------------------------------------------------
NGHIÊN CỨU CÔNG NGHỆ MULTI-EDGE COORDINATION
ỨNG DỤNG TRONG XỬ LÝ NHIỀU CAMERA GIAO THÔNG

-------------------------------------------------------------------------------------
Sinh viên thực hiện: Đàm Vũ Đức Anh — Lớp Mạng máy tính và truyền thông dữ liệu, Khóa 57
Ngành: Mạng máy tính — Chuyên ngành: Mạng máy tính và truyền thông dữ liệu
Giảng viên hướng dẫn: Trung tá, Th.S Đặng Lê Đình Trang
Năm học: 2025 – 2026

-------------------------------------------------------------------------------------
Hà Nội, ngày ……… tháng ……… năm 2026

LUẬN VĂN TỐT NGHIỆP
NGÀNH: Mạng máy tính
CHUYÊN NGÀNH: Mạng máy tính và truyền thông dữ liệu

-------------------------------------------------------------------------------------
CHỦ ĐỀ: NGHIÊN CỨU CÔNG NGHỆ MULTI-EDGE COORDINATION
ỨNG DỤNG TRONG XỬ LÝ NHIỀU CAMERA GIAO THÔNG
MÃ SỐ:
NGÀY GIAO: ……/……/…… — NGÀY HOÀN THÀNH: ……/……/……

-------------------------------------------------------------------------------------
CĂN CỨ THỰC HIỆN:
Họ và tên: Đàm Vũ Đức Anh
Lớp: Mạng máy tính và truyền thông dữ liệu, Khóa 57
Ngành: Mạng máy tính
Chuyên ngành: Mạng máy tính và truyền thông dữ liệu
Tên đề tài: Nghiên cứu công nghệ Multi-Edge Coordination ứng dụng trong xử lý nhiều camera giao thông.
Số lượng, nội dung bản vẽ: Không có bản vẽ.
Cán bộ hướng dẫn:
Họ và tên: TS. Đặng Lê Đình Trang
Cấp bậc: Trung tá
Chức vụ: Phó chủ nhiệm bộ môn
Đơn vị: Bộ môn An toàn thông tin - Công nghệ mạng - Viện Công nghệ thông tin và Truyền thông - Học viện Kỹ thuật quân sự.

-------------------------------------------------------------------------------------
Tóm tắt

Trong bối cảnh đô thị hóa nhanh chóng và sự gia tăng lưu lượng giao thông, các hệ
thống giám sát giao thông đòi hỏi khả năng xử lý đồng thời nhiều camera với độ trễ
thấp, độ chính xác cao và tính sẵn sàng liên tục. Các giải pháp truyền thống thường
truyền toàn bộ video về trung tâm, gây tăng băng thông, tăng độ trễ, và tạo ra một
điểm lỗi duy nhất. Hướng tiếp cận xử lý tại biên (Edge Computing) kết hợp trí tuệ nhân
tạo tại biên (Edge AI) cho phép mỗi thiết bị thực hiện phân tích AI trực tiếp tại
hiện trường và chỉ gửi các sự kiện quan trọng về trung tâm.

Tuy nhiên, khi mở rộng số lượng camera, một thiết bị Edge đơn lẻ sẽ dễ bị quá tải,
dẫn đến FPS giảm và hệ thống mất ổn định. Đề tài đề xuất kiến trúc Multi-Edge
Coordination phân tán: các thiết bị Jetson Edge giao tiếp trực tiếp qua giao thức
Eclipse Zenoh ở chế độ peer-to-peer, chia sẻ trạng thái, cân bằng tải, và phục hồi
tự động khi một node bị sự cố — hoàn toàn không cần server điều phối trung tâm.

Hệ thống được triển khai dựa trên pipeline DeepStream đa luồng trên NVIDIA Jetson,
với cơ chế thu thập telemetry thống nhất (FPS, feature counts, session_id, sequence)
được ghi nguyên tử và mọi thành phần đều kiểm tra tính tươi và thứ tự của snapshot
trước khi sử dụng. HealthAgent, profile_collect và PeerOrchestrator đều dùng cùng
một nguồn snapshot duy nhất. PeerOrchestrator sử dụng điểm số tải dựa trên FPS để
quyết định tăng dần offload, chuyển giao camera, thu hồi và cứu hộ khi node offline.

Phiên bản hiện tại là Baseline phản ứng (reactive): cấu hình LOAD_POLICY=actual,
mô hình tải là công thức, proactive mode = disabled, và không hề triển khai bất kỳ
bộ dự báo học sâu nào trên Jetson. Đề tài phân biệt rõ các cơ chế đã triển khai và
các thí nghiệm dự báo trong tương lai, chỉ trình bày những bằng chứng có thể truy
nguồn trực tiếp về code, config, tests, hoặc dữ liệu thu thập thực tế.

Từ khóa: Edge AI, Multi-Edge Coordination, NVIDIA Jetson, DeepStream, Eclipse
Zenoh, peer-to-peer, giám sát giao thông, migration camera.

-------------------------------------------------------------------------------------
Lời cảm ơn

Sinh viên xin gửi lời cảm ơn chân thành nhất đến:

- TS. Trung tá Đặng Lê Đình Trang — giảng viên hướng dẫn, đã tận hết tâm huyết
  chỉ dạy và đồng hành suốt quá trình nghiên cứu.

- Các thầy giáo, cô giáo và bạn bè trong Bộ môn An toàn thông tin — Công nghệ
  mạng — Viện Công nghệ thông tin và Truyền thông, Học viện Kỹ thuật Quân sự.

- Gia đình và người thân — luôn động viên và tin tưởng.

-------------------------------------------------------------------------------------
DANH MỤC TỪ VIẾT TẮT

| STT | Từ viết tắt | Ý nghĩa                                                |
| --- | ----------- | ------------------------------------------------------ |
| 1   | AI          | Artificial Intelligence — Trí tuệ nhân tạo             |
| 2   | API         | Application Programming Interface — Giao diện lập trình ứng dụng |
| 3   | CPU         | Central Processing Unit — Đơn vị xử lý trung tâm       |
| 4   | CUDA        | Compute Unified Device Architecture — Kiến trúc tính toán GPU |
| 5   | FPS         | Frames Per Second — Số khung hình trên giây            |
| 6   | GPU         | Graphics Processing Unit — Đơn vị xử lý đồ họa         |
| 7   | IP          | Internet Protocol — Giao thức Internet                 |
| 8   | LPD         | License Plate Detection — Phát hiện biển số            |
| 9   | LPR         | License Plate Recognition — Nhận dạng biển số          |
| 10  | P2P         | Peer-to-Peer — Ngang hàng                              |
| 11  | ROI         | Region of Interest — Vùng quan tâm                   |
| 12  | RTSP        | Real-Time Streaming Protocol — Giao thức truyền phát thời gian thực |
| 13  | SGIE        | Secondary GIE — Bộ suy luận thứ cấp trong DeepStream  |
| 14  | YOLO        | You Only Look Once — Mô hình phát hiện đối tượng thời gian thực |

-------------------------------------------------------------------------------------
MỤC LỤC

LỜI CẢM ƠN .............................................................................................................. 2
DANH MỤC TỪ VIẾT TẮT ............................................................................................... 3
DANH MỤC HÌNH VẼ ................................................................................................. 5
DANH MỤC BẢNG ....................................................................................................... 5
MỞ ĐẦU ................................................................................................................ 6
CHƯƠNG 1: TỔNG QUAN ĐỀ TÀI VÀ CƠ SỞ LÝ THUYẾT ................................. 7
1.1. Giới thiệu bài toán Multi-Edge traffic monitoring ............................. 7
1.2. Kiến trúc Edge AI trên NVIDIA Jetson .......................................... 9
1.3. Giao thức Zenoh phân tán không broker .......................................... 12
1.4. Các kỹ thuật thị giác máy tính nền tảng ......................................... 15
1.5. Kết luận chương 1 ....................................................................... 19
CHƯƠNG 2: PHÂN TÍCH VÀ THIẾT KẾ GIẢI PHÁP MULTI-EDGE COORDINATION .... 21
2.1. Phân tích yêu cầu hệ thống ................................................. 21
2.2. Thiết kế kiến trúc hệ thống ..................................................... 24
2.3. Thiết kế hệ thống telemetry và load score .................................... 28
2.4. Thiết kế máy trạng và cơ chế điều phối ................................... 31
2.5. Thiết kế Server giám sát và Dashboard ................................. 36
2.6. Kết luận chương 2 .................................................................... 40
CHƯƠNG 3: XÂY DỰNG, PHÁT TRIỂN VÀ ĐÁNH GIÁ HỆ THỐNG ...................... 42
3.1. Triển khai Edge runtime và pipeline DeepStream .......................... 42
3.2. Hệ thống thu thập telemetry và data contract ............................... 46
3.3. Triển khai PeerOrchestrator P2P .......................................... 49
3.4. Phương pháp thực nghiệm .................................................. 52
3.5. Kết quả và phân tích ..................................................... 56
3.6. Hạn chế và hướng phát triển ............................................... 60
KẾT LUẬN VÀ HƯỚNG PHÁT TRIỂN .................................................................... 63
TÀI LIỆU THAM KHẢO ................................................................................. 66

-------------------------------------------------------------------------------------
DANH MỤC HÌNH VẼ

Hình 1.1. Mô hình triển khai Multi-Edge phân tán ................................. 8
Hình 1.2. Luồng xử lý DeepStream trên một Edge node ....................... 10
Hình 1.3. Vòng đời telemetry snapshot thống nhất ........................... 13
Hình 2.1. Sơ đồ kiến trúc tổng thể hệ thống ..................................... 24
Hình 2.2. Pipeline DeepStream với các probes ..................................... 27
Hình 2.3. Luồng điều phối P2P và camera migration ......................... 31
Hình 2.4. Dashboard Cluster Status ............................................. 37
Hình 3.1. Cấu trúc dòng lệnh run_edge.sh ....................................... 43
Hình 3.2. Quy trình thu thập và kiểm tra dữ liệu ............................ 47
Hình 3.3. Kết quả tập hợp test suite ........................................... 53

-------------------------------------------------------------------------------------
DANH MỤC BẢNG

Bảng 1.1. Danh sách cấu hình hệ thống Edge (Edge/.env, configs/edge_node.yml) ... 11
Bảng 1.2. Các ngưỡng load score phản ứng (reactive) ............................. 16
Bảng 2.1. Zenoh Key Expressions ................................................ 32
Bảng 2.2. Các trường telemetry chính trong snapshot .............................. 29
Bảng 3.1. Cấu hình runtime Edge hiện tại ........................................ 49
Bảng 3.2. Các kịch bản kiểm thử hệ thống ......................................... 54

-------------------------------------------------------------------------------------
MỞ ĐẦU

1. Lý do chọn đề tài

Hạ tầng giao thông đô thị ngày càng phụ thuộc vào các hệ thống giám sát bằng camera.
Một nút giao lớn không chỉ cần xem lại video sau khi sự kiện xảy ra, mà còn cần xử lý
trực tiếp tại thời điểm sự kiện đang diễn ra: phát hiện phương tiện, theo dõi quỹ đạo,
nhận dạng biển số, ước lượng tốc độ, ghi nhận vi phạm và hiển thị trạng thái cho người
vận hành. Khi số lượng camera tăng từ một vài luồng lên nhiều luồng đồng thời, bài toán
không còn là chạy một mô hình AI trên một video, mà trở thành bài toán duy trì chất
lượng dịch vụ của cả một cụm xử lý video liên tục.

Cách triển khai tập trung truyền toàn bộ video về một máy chủ trung tâm có ưu điểm là
dễ quản lý và dễ quan sát. Tuy nhiên, mô hình này bộc lộ nhiều hạn chế khi áp dụng tại
hiện trường. Thứ nhất, luồng video độ phân giải cao tạo ra nhu cầu băng thông lớn, đặc
biệt khi nhiều camera cùng truyền về một điểm. Thứ hai, độ trễ phụ thuộc vào đường
truyền mạng, trong khi các tác vụ giám sát giao thông cần phản hồi gần thời gian thực.
Thứ ba, máy chủ trung tâm và đường truyền trở thành điểm lỗi tập trung; nếu một mắt
xích bị lỗi, nhiều camera có thể cùng mất khả năng giám sát.

Edge AI là hướng tiếp cận tự nhiên để giảm các hạn chế trên. Thay vì gửi toàn bộ dữ
liệu thô về trung tâm, mỗi thiết bị biên đặt gần camera có thể giải mã video, chạy mô
hình phát hiện, theo dõi đối tượng, nhận dạng biển số và chỉ gửi kết quả hoặc luồng
đã xử lý. NVIDIA Jetson cùng DeepStream cung cấp một nền tảng phù hợp cho hướng này
vì thiết bị có GPU, phần cứng giải mã video, TensorRT và pipeline GStreamer tối ưu cho
phân tích nhiều luồng. Nhờ vậy, việc xử lý có thể diễn ra gần nguồn dữ liệu hơn, giảm
độ trễ và giảm áp lực cho mạng truyền dẫn.

Tuy nhiên, đưa xử lý xuống Edge không làm bài toán biến mất; nó chuyển bài toán từ
"một máy chủ trung tâm quá tải" thành "nhiều thiết bị biên có tài nguyên hữu hạn".
Một Jetson có thể xử lý ổn định trong một cấu hình camera nhất định, nhưng FPS có thể
giảm khi số luồng tăng, cảnh giao thông đông hơn, nhiệt độ tăng, hoặc một nguồn RTSP
không ổn định làm pipeline phải reconnect. Trong hệ thống giao thông, FPS không phải
chỉ là chỉ số phụ. FPS thấp làm mất độ mịn của quỹ đạo, tăng nguy cơ bỏ sót biển số,
làm sai lệch đo tốc độ và làm dashboard phản ánh tình trạng hiện trường chậm hơn thực
tế.

Vì vậy vấn đề nghiên cứu của luận văn là: làm thế nào để nhiều Edge node có thể tự
phối hợp nhằm duy trì QoS khi xử lý nhiều camera giao thông, nhưng không phụ thuộc
vào một bộ điều khiển trung tâm. Mỗi node phải tự đo trạng thái của chính nó, chia sẻ
trạng thái với peer, quyết định khi nào cần offload, chọn camera hoặc crop phù hợp,
thực hiện migration an toàn, thu hồi camera khi tải giảm và cứu camera orphan khi một
peer mất kết nối. Đồng thời, hệ thống phải sinh ra bằng chứng vận hành đủ sạch: mỗi
snapshot telemetry cần có `session_id`, `sequence`, thời gian window, FPS, input FPS,
feature counts và load score để tránh nhầm dữ liệu cũ thành dữ liệu mới.

Độ khó của bài toán nằm ở chỗ các tín hiệu quan sát được không hoàn toàn đáng tin nếu
dùng riêng lẻ. GPU utilization trên Jetson có thể bị ảnh hưởng bởi TensorRT burst,
DVFS và cách lấy mẫu GR3D; một camera có output FPS thấp có thể do nguồn RTSP yếu chứ
không phải do nó gây tải lớn; producer log báo đã gửi health chưa chứng minh Server
và dashboard đã nhận được health. Do đó giải pháp không thể chỉ thêm một threshold
đơn giản. Nó cần một contract telemetry rõ ràng, một load score ưu tiên FPS, một state
machine có dwell/cooldown để tránh ping-pong và một quy trình kiểm chứng dựa trên
receiving-side evidence.

2. Mục tiêu, đối tượng và phạm vi nghiên cứu

Mục tiêu của đề tài là xây dựng một runtime Multi-Edge Coordination cho giám sát giao
thông bằng camera, trong đó nhiều Edge node có thể tự đo tải, trao đổi trạng thái,
điều phối offload/migration và khôi phục camera khi peer mất kết nối. Trọng tâm của
đề tài là duy trì QoS của pipeline video, đặc biệt là FPS, thay vì tối ưu một chỉ số
tài nguyên riêng lẻ như GPU utilization.

Đối tượng nghiên cứu là hệ thống Edge AI xử lý nhiều camera giao thông trên NVIDIA
Jetson, gồm pipeline DeepStream, telemetry runtime, health publishing, cơ chế P2P
coordination, Server quan sát và dashboard. Các bài toán thị giác máy tính như phát
hiện phương tiện, theo dõi, nhận dạng biển số và đo tốc độ được xem là workload của
pipeline; luận văn không tập trung đề xuất mô hình detection/recognition mới.

Phạm vi nghiên cứu của báo cáo là reactive Multi-Edge runtime đang tồn tại trong hệ
thống. Phần proactive/DL predictor được trình bày như hướng phát triển vì runtime hiện
chưa bật nhánh này làm policy triển khai. Báo cáo cũng không khẳng định các hành vi
Jetson-only đã được xác nhận nếu mới có host-only tests; các kết luận về migration,
failover, L2/L3 crop offload và receiving-side dashboard phải đi kèm điều kiện kiểm
chứng tương ứng.

Các giả định triển khai gồm: các Edge node nằm trong cùng LAN hoặc miền mạng cho phép
Zenoh peer discovery; mỗi node có `NODE_ID` ổn định; mỗi camera có `camera_id`, URI,
ROI, homography và speed limit rõ ràng; camera config đủ để peer khác mở lại camera
khi migration hoặc rescue; Server là observer, không phải central controller.

3. Phương pháp nghiên cứu

Đề tài được thực hiện theo hướng phân tích hệ thống thật, thiết kế contract runtime,
hiện thực module và kiểm chứng theo bằng chứng. Trước hết, hệ thống được mô hình hóa
theo đầu vào, đầu ra, giả định và ràng buộc QoS. Tiếp theo, telemetry snapshot, load
score và PeerOrchestrator được thiết kế để chuyển tín hiệu đo thô thành quyết định
điều phối. Sau đó, các module runtime được đối chiếu với code/config hiện tại để đảm
bảo báo cáo không mô tả vượt quá hệ thống đang có. Cuối cùng, phần đánh giá tách rõ
test suite, baseline cần đo, receiving-side evidence và những giới hạn chưa được phần
cứng xác nhận.

4. Đóng góp của đề tài

Luận văn xây dựng và chuẩn hóa một hệ thống Multi-Edge traffic monitoring dựa trên
runtime thực tế trong repository. Các đóng góp chính gồm:

1. Xây dựng Edge runtime chạy DeepStream pipeline với nhiều camera, YOLO PGIE,
   NvDCF tracker, LPD/LPR, analytics probe, OSD và RTSP push output.
2. Thiết kế unified telemetry snapshot tại `/dev/shm/speedflow_fps.json`, có
   `session_id`, `sequence`, window duration, FPS per camera, input FPS, feature
   counts, crop counters và load score breakdown.
3. Xây dựng HealthAgent publish health qua Zenoh và WebSocket, dùng FPS-first load
   score thay vì GPU-trigger policy.
4. Triển khai PeerOrchestrator phân tán cho multi-level offload: L3 plate crop,
   L2 vehicle crop, L1 full stream migration, reclaim và peer-offline rescue.
5. Chuẩn hóa Server observer path gồm edge registry, WebSocket dashboard, REST API,
   offline watchdog và stream/snapshot endpoints.
6. Xác lập data collection contract qua `run_edge --collect --collect-interval 1.0`,
   trong đó `profile_collect.py` tự reject stale/duplicate snapshot trước khi ghi CSV.
7. Phân định rõ phần đã chạy trong runtime reactive và phần mới là hướng phát triển:
   proactive/DL predictor có code nhưng chưa được bật làm policy triển khai.

Điểm nhấn của đóng góp không phải một thuật toán học sâu mới, mà là một runtime
coordination có thể kiểm chứng. Hệ thống hiện tại là reactive baseline: quyết định
được dẫn bởi load score đo trực tiếp, không bởi mô hình dự báo chưa triển khai.

5. Cấu trúc báo cáo

Chương 1 trình bày bối cảnh xử lý video giao thông tại biên, nền tảng NVIDIA Jetson,
DeepStream, Zenoh và các kỹ thuật thị giác máy tính cần thiết để hiểu bài toán. Phần
này chỉ chọn những kiến thức trực tiếp phục vụ thiết kế telemetry, FPS, P2P
communication và camera migration.

Chương 2 phân tích yêu cầu và trình bày giải pháp Multi-Edge Coordination. Chương này
xác định đầu vào, đầu ra, giả định triển khai, kiến trúc tổng thể, contract telemetry,
load score, state machine offload/reclaim/failover và vai trò quan sát của Server.
Các cơ chế được sắp xếp theo chuỗi: bài toán, yêu cầu, kiến trúc, telemetry, load
score, state machine và vai trò quan sát của Server.

Chương 3 chuyển từ thiết kế sang triển khai và đánh giá. Nội dung được tách thành ba
tầng: xây dựng hệ thống, phương pháp thực nghiệm, kết quả và phân tích. Phần kết luận
cuối cùng quay lại mục tiêu ban đầu, tổng hợp đóng góp, nêu hạn chế và đề xuất hướng
phát triển xuất phát từ các hạn chế thực tế của hệ thống.

-------------------------------------------------------------------------------------
CHƯƠNG 1: TỔNG QUAN ĐỀ TÀI VÀ CƠ SỞ LÝ THUYẾT

1.1. Giới thiệu bài toán Multi-Edge Traffic Monitoring

Vận tải thông minh đặt ra yêu cầu xử lý đồng thời nhiều camera tại một núi giao
thông với độ trễ thấp, độ chính xác cao và khả năng hoạt động liên tục 24/7. Tại
một núi giao thông kiểu Việt Nam, hệ thống cần giám sát 4-8 camera, phát hiện
phương tiện vượt tốc độ, nhận dạng biển số và phản hồi gần như tức thời. Các yêu
cầu then chốt bao gồm: xử lý thời gian thực với FPS ≥ 25/camera, độ chính xác
đo tốc độ ≤ ±5% so với giá trị thực, tính sẵn sàng 24/7 với khả năng tự phục hồi,
và khả năng mở rộng thêm/bớt camera và Edge node mà không ảnh hưởng toàn
hệ thống.

Một thiết bị Edge đơn lẻ (ví dụ: Jetson AGX Orin) có giới hạn về số lượng
luồng video xử lý đồng thời với FPS tối ưu. Khi số lượng camera tăng hoặc mật
độ phương tiện cao điểm (giờ cao điểm, đèn đỏ tạo hàng dài), GPU bão hòa,
FPS giảm và hệ thống mất ổn định. Vì vậy, cần phân phối camera cho nhiều
node Edge, nhưng điều này tạo ra thách thức: các node cần biết ai đang quá tải,
ai có thể tiếp nhận, và cách chuyển giao camera mà không mất khung hình.

Từ góc nhìn nghiên cứu, bài toán này nằm ở giao điểm của ba nhóm vấn đề. Nhóm thứ
nhất là xử lý video thời gian thực: pipeline phải duy trì throughput đủ cao để không
làm đứt mạch quan sát. Nhóm thứ hai là tính sẵn sàng của hệ phân tán: khi một node
quá tải hoặc mất kết nối, các node còn lại phải tiếp tục cung cấp dịch vụ thay vì chờ
một controller tập trung. Nhóm thứ ba là đo lường và kiểm chứng: quyết định điều phối
chỉ có ý nghĩa nếu trạng thái tải được đo đúng và nếu kết quả được xác nhận tại phía
nhận. Ba nhóm này khiến bài toán khác với một bài toán computer vision thuần túy.

1.1.1. Bối cảnh xử lý video giao thông tại biên

Trong một hệ thống camera giao thông, dữ liệu đầu vào có tính liên tục và khó dự báo.
Mỗi camera gửi video theo thời gian thực, nhưng mật độ phương tiện thay đổi theo giờ,
theo chu kỳ đèn tín hiệu, theo thời tiết và theo các tình huống bất thường như ùn tắc
hoặc tai nạn. Nếu hệ thống chỉ xử lý từng frame độc lập, nó có thể phát hiện phương
tiện tại một thời điểm, nhưng chưa đủ để đảm bảo chất lượng giám sát liên tục. Hệ
thống cần duy trì quỹ đạo đối tượng, cần đủ frame để ước lượng tốc độ và cần đủ ổn
định để dashboard không bị gián đoạn.

Xử lý tại biên giúp giảm độ trễ vì dữ liệu được phân tích ngay gần camera. Tuy nhiên,
thiết bị biên không có tài nguyên vô hạn. Jetson có GPU và bộ giải mã video chuyên
dụng, nhưng CPU, RAM, nhiệt độ, băng thông nội bộ và khả năng encode output vẫn có
giới hạn. Khi nhiều camera cùng chạy detector, tracker và nhận dạng biển số, hệ thống
có thể bước qua một ngưỡng mà FPS giảm nhanh. Điểm này rất quan trọng: pipeline video
thường không suy giảm tuyến tính. Nó có thể ổn định ở gần 25 FPS trong một khoảng tải,
rồi rơi mạnh khi tài nguyên bị bão hòa.

Do đó, luận văn chọn FPS làm tín hiệu QoS trung tâm. CPU/GPU/RAM vẫn được thu thập để
quan sát, nhưng mục tiêu vận hành cuối cùng là giữ pipeline đủ frame để không mất khả
năng giám sát. Một hệ thống có GPU% thấp nhưng FPS đã giảm vẫn là hệ thống có vấn đề;
ngược lại, GPU% cao trong một burst ngắn nhưng FPS vẫn ổn định chưa đủ để kết luận
cần migration.

1.1.2. Hạn chế của mô hình tập trung

Mô hình tập trung thường gom video từ nhiều camera về một server xử lý. Cách này phù
hợp khi số camera ít, mạng ổn định và server đủ mạnh. Tuy nhiên trong triển khai giao
thông thực tế, mô hình này gặp bốn hạn chế chính.

Thứ nhất, video thô tiêu tốn băng thông lớn. Một camera 1080p/25-30 FPS đã tạo ra
luồng dữ liệu đáng kể; nhiều camera cùng truyền về trung tâm sẽ làm uplink trở thành
nút cổ chai. Thứ hai, độ trễ phụ thuộc vào mạng. Nếu đường truyền dao động, thời điểm
xử lý tại server không còn sát thời điểm sự kiện xảy ra. Thứ ba, server trung tâm là
điểm lỗi tập trung. Khi server hoặc đường truyền đến server lỗi, nhiều camera cùng mất
khả năng xử lý. Thứ tư, việc mở rộng hệ thống yêu cầu tăng năng lực server trung tâm,
dẫn đến chi phí phần cứng và vận hành lớn.

Multi-Edge Coordination không phủ nhận vai trò của server, nhưng thay đổi vai trò đó.
Server trong đề tài chỉ làm nhiệm vụ quan sát, lưu trạng thái và phục vụ dashboard.
Policy điều phối nằm ở Edge node. Nhờ vậy, khi Server dashboard lỗi, các Edge vẫn có
thể tiếp tục trao đổi heartbeat P2P và tự xử lý migration/failover. Đây là điểm khác
biệt cốt lõi giữa hệ giám sát có dashboard và hệ điều phối phụ thuộc controller.

1.1.3. Khó khăn của cân bằng tải trong pipeline video

Cân bằng tải trong pipeline video khác với cân bằng tải request HTTP. Trong HTTP,
mỗi request thường độc lập và có thời gian sống ngắn; load balancer có thể chuyển
request mới sang server khác mà không cần di chuyển trạng thái dài hạn. Với camera,
một luồng RTSP là trạng thái liên tục. Nó gắn với decoder, muxer, tracker, source_id,
homography, ROI, output layout và dữ liệu overlay. Chuyển một camera sang node khác
không chỉ là đổi một endpoint; nó là thao tác thay đổi pipeline đang chạy.

Ngoài ra, camera không có cùng chi phí xử lý. Một camera nhìn vào làn đường vắng sinh
ít object; một camera nhìn vào nút giao đông sinh nhiều track và nhiều crop biển số.
Vì vậy, chọn camera để offload không thể chỉ dựa vào số camera. Hệ thống cần biết
workload theo camera. Trong hệ thống hiện tại, workload camera được dẫn từ
`n_track+n_plate`, sau đó dùng để chọn candidate: L1 full-stream migration chọn camera
workload thấp nhất để giảm rủi ro chuyển một camera quá nặng; L2/L3 crop offload chọn
camera workload cao nhất vì crop offload chỉ có ý nghĩa nếu camera đó sinh nhiều crop.

Một khó khăn khác là tránh dao động. Nếu policy thấy FPS giảm rồi lập tức chuyển
camera, sau đó FPS tăng và lập tức reclaim, hệ thống sẽ rơi vào ping-pong. Vì vậy
state machine cần dwell time, cooldown và reclaim stability. Những tham số này làm
hệ thống phản ứng chậm hơn một chút, nhưng đổi lại tránh được việc liên tục add/remove
stream trong một pipeline GStreamer đang chạy.

1.1.4. Câu hỏi nghiên cứu và phạm vi luận văn

Từ các phân tích trên, câu hỏi trung tâm của luận văn là: có thể xây dựng một cơ chế
điều phối nhiều Edge node theo kiểu phân tán, dựa trên telemetry có thể kiểm chứng,
để duy trì QoS cho xử lý nhiều camera giao thông hay không. Câu hỏi này được chia
thành bốn câu hỏi nhỏ hơn.

Thứ nhất, hệ thống cần đo trạng thái pipeline như thế nào để tránh dùng dữ liệu cũ,
dữ liệu trùng hoặc dữ liệu không có identity. Thứ hai, load score nên được xây dựng
như thế nào để phản ánh đúng chất lượng dịch vụ, tránh phụ thuộc mù vào GPU%. Thứ ba,
state machine điều phối cần những bước nào để migration, reclaim và failover không
gây thêm bất ổn. Thứ tư, bằng chứng nào đủ để kết luận cơ chế hoạt động: producer log,
unit test, Server receiving, dashboard receiving hay đo end-to-end trên Jetson.

Phạm vi luận văn tập trung vào reactive Multi-Edge Coordination. Các thành phần dự
báo như `DLPredictor` và proactive model được ghi nhận như hướng phát triển vì runtime
hiện tại chưa bật chúng làm policy triển khai. Cách giới hạn phạm vi này giúp luận văn
không phóng đại kết quả và giữ lập luận gắn với hệ thống thực tế.

1.2. Kiến trúc Edge AI trên NVIDIA Jetson

1.2.1. Nền tảng phần cứng NVIDIA Jetson

NVIDIA Jetson là dòng máy tính nhúng hiệu năng cao, tích hợp GPU NVIDIA (CUDA
cores, Tensor Cores), CPU ARM đa lõi, bộ mã hoá/giải mã video phần cứng (NVENC/
NVDEC), ISP xử lý hình ảnh và bộ nhớ nhanh. Đối với bài toán xử lý nhiều camera
giao thông, Jetson AGX Orin cho phép chạy đồng thời nhiều mô hình AI (YOLO +
LPD + LPR) trên nhiều luồng video RTSP với tổng FPS cao.

1.2.2. NVIDIA DeepStream SDK

DeepStream SDK là framework phân tích video thời gian thực của NVIDIA, xây dựng
trên nền tảng GStreamer. Thay vì viết hàng ngàn dòng mã để quản lý luồng video,
giải mã, quản lý bộ nhớ GPU, đồng bộ hóa và suy luận AI, DeepStream cung cấp
kiến trúc plugin cho phép lắp ghép các thành phần (source, muxer, inference,
tracker, analytics, renderer, sink) thành một pipeline hoàn chỉnh.

Một pipeline DeepStream tiêu biểu cho bài toán xử lý đa camera giao thông bao
gồm các bước chính:

1. uridecodebin — Nhận luồng RTSP từ camera IP, giải mã hardware (NVDEC).
2. nvstreammux — Ghép N luồng thành một batch để xử lý GPU hiệu quả.
3. nvinfer (PGIE) — Chạy YOLO detector phát hiện phương tiện.
4. nvtracker — Theo dõi đối tượng với NvDCF, gán unique ID.
5. nvinfer (SGIE-LPD) — Phát hiện vùng biển số.
6. nvinfer (SGIE-LPR) — Nhận dạng ký tự biển số (OCR).
7. nvdsanalytics — Phân tích ROI, cung cấp pad cho SpeedProbe.
8. nvdsosd — Vẽ bounding box, speed overlay, plate text.
9. nvmultistreamtiler — Ghép N camera thành grid.
10. rtspclientsink / nveglglessink — đẩy RTSP hoặc hiển thị.

Ưu điểm cốt lõi của DeepStream: dữ liệu video chỉ được nạp vào GPU một lần,
tất cả bước xử lý (giải mã, inference, theo dõi, vẽ overlay) đều chạy trên GPU,
giảm thiểu copy giữa CPU và GPU. Pipeline được xây dựng hoàn toàn bằng Python
thông qua GObject introspection (gi.repository.Gst), cho phép điều chỉnh
động nguồn.

1.2.3. Tối ưu hóa với TensorRT

TensorRT là bộ công cụ tối ưu hóa inference của NVIDIA. Mô hình AI (YOLO, LPD,
LPR) sau khi huấn luyện được chuyển đổi sang TensorRT engine (.engine), áp dụng
các kỹ thuật:

- Quantization (FP32 → FP16 → INT8): giảm độ chính xác số học để tăng tốc.
- Layer Fusion: hợp nhất lớp Conv + Bias + ReLU thành CBR.
- Kernel Auto-Tuning: chọn kernel CUDA tối ưu.
- Dynamic Batch Size: điều chỉnh batch size theo số lượng camera.

TensorRT có vai trò quan trọng trong hệ thống vì các mô hình YOLO, LPD và LPR phải
chạy lặp lại trên nhiều frame. Nếu inference quá chậm, mọi cơ chế điều phối phía sau
đều chỉ xử lý hậu quả. Tuy nhiên, TensorRT không giải quyết toàn bộ bài toán. Một
pipeline DeepStream còn có decode, mux, tracking, analytics, OSD, encode và network
sink. Vì vậy, luận văn không dùng tốc độ inference riêng lẻ làm thước đo chính. Thước
đo vận hành được chọn là FPS của toàn pipeline sau khi đã đi qua các stage xử lý.

1.2.4. GStreamer và ý nghĩa của pipeline runtime

GStreamer là framework xử lý multimedia theo mô hình pipeline. Một pipeline gồm các
element được nối bằng pad, dữ liệu chảy từ source đến sink. DeepStream xây dựng trên
GStreamer và bổ sung các plugin tối ưu cho GPU NVIDIA. Đối với đề tài này, hiểu
GStreamer là cần thiết vì camera migration không chỉ thay đổi một cấu hình logic mà
phải tác động vào pipeline đang PLAYING.

Khi thêm một camera, hệ thống phải tạo source bin, nối source vào streammux, đồng bộ
state của element mới với pipeline cha và đảm bảo metadata source_id khớp với cấu
hình. Khi xóa một camera, hệ thống phải block pad, unlink, release request pad, set
element về NULL rồi mới remove. Nếu thao tác này làm sai thread hoặc sai thứ tự,
pipeline có thể deadlock, crash hoặc rò tài nguyên. Vì vậy trong triển khai thực tế,
các thao tác add/remove được đưa vào GLib main loop bằng `GLib.idle_add` để chạy đúng
ngữ cảnh GStreamer.

1.2.5. Liên hệ giữa Edge AI và bài toán điều phối

Edge AI cung cấp khả năng xử lý cục bộ, nhưng không tự động cung cấp coordination.
Một Jetson có thể tự chạy detector, nhưng nó không tự biết peer nào đang rảnh, peer
nào vừa offline, camera nào nên chuyển đi, hoặc khi nào nên thu hồi camera. Do đó,
phần AI pipeline và phần coordination phải được thiết kế như hai lớp liên kết.

Lớp pipeline tạo ra dữ liệu: FPS, feature counts, input FPS, crop counters, camera
configs và trạng thái active cameras. Lớp coordination tiêu thụ dữ liệu đó để ra quyết
định. Nếu dữ liệu pipeline không có identity hoặc stale, coordination có thể ra quyết
định sai. Nếu coordination không hiểu ràng buộc pipeline, nó có thể remove source quá
sớm hoặc add source sai thread. Mối liên hệ này là lý do luận văn dành dung lượng đáng
kể cho telemetry contract và Make-Before-Break thay vì chỉ mô tả mô hình AI.

1.3. Giao thức Eclipse Zenoh phân tán không broker

Eclipse Zenoh là giao thức mã nguồn mở thiết kế cho ứng dụng Edge, Robotics,
và IoT. Zenoh hỗ trợ mô hình giao tiếp đa dạng và các chế độ hoạt động khác
nhau:

- Pub/Sub: publisher gửi dữ liệu đến key expression; subscriber nhận.
- Query/Reply: gửi query đến key, handler trả lời.
- Key Expression: key phân cấp, hỗ trợ wildcard.
- Peer mode: hoàn toàn P2P, không cần router, tự discovery qua UDP
  multicast scouting.
- Router mode: router đóng vai trò relay.
- Client mode: kết nối đến router.

Đối với Multi-Edge, Zenoh peer mode với UDP multicast scouting cho phép các
Jetson trong cùng LAN tự khám phá nhau ngay khi khởi động. Message serialization
nhị phân với msgpack giảm overhead, phù hợp với heartbeat thông tin metrics.

Hệ thống sử dụng Zenoh như một lớp truyền tin P2P. Chính sách điều phối vẫn
nằm tại mỗi Edge node — Server chỉ quan sát và hiển thị, không ra quyết định
chuyển giao camera. Các nhóm message chính bao gồm: trạng thái health, lệnh
điều khiển camera (ADD/REMOVE), ack migration, sự kiện vi phạm, và kết quả
crop offload.

Zenoh được chọn vì bài toán cần trao đổi trạng thái nhẹ, liên tục và phân tán. Một
heartbeat mỗi giây không nên phải đi qua một broker trung tâm nếu các Jetson nằm cùng
LAN và có thể tự discovery. Peer mode làm giảm phụ thuộc hạ tầng, đồng thời giữ được
mô hình key expression rõ ràng để phân biệt status, control command, ack và offload
crop. Với msgpack, payload không cần chuyển sang JSON text trong đường P2P, giảm chi
phí serialize cho các thông điệp lặp lại.

Điểm quan trọng là Zenoh chỉ giải quyết bài toán truyền tin. Nó không tự giải quyết
bài toán nhất quán policy. Vì không có leader, mỗi node phải có cùng quy tắc xử lý
offline peer, cùng timeout, cùng cách chọn rescuer và cùng điều kiện nhận camera. Nếu
message layer tốt nhưng state machine không rõ, hệ thống vẫn có thể double-add camera
hoặc không node nào chịu rescue. Do đó, phần thiết kế sau phải gắn Zenoh với state
machine cụ thể, không dừng ở việc nói "dùng P2P".

1.4. Các kỹ thuật thị giác máy tính nền tảng

Pipeline xử lý giao thông bao gồm: detector YOLO, tracker NvDCF, xử lý biển số
(LPD/LPR), lọc ROI, và ước tính tốc độ qua homography. SpeedProbe (một GStreamer
pad probe) tiêu thụ metadata, duy trì lịch sử theo dõi, áp dụng ma trận
homography và quy tắc xác thực, đồng thời xuất bản vi phạm.

Đối với điều phối, FPS là tín hiệu chất lượng dịch vụ chính — được tin cậy nhất.
GPU utilization không được dùng làm tín hiệu trigger độc lập vì các burst ngắn,
clock scaling, và hành vi lấy mẫu riêng biệt của Tegra có thể gây hiểu lầm. Các
tín hiệu workload và nhiệt độ chỉ góp phần thưởng cộng dồn, không thay thế cho
đường cơ sở FPS.

1.4.1. Phát hiện phương tiện và tracking

Detector YOLO cung cấp bounding box và class của phương tiện trong từng frame. Nếu chỉ
có detector, hệ thống biết rằng có phương tiện xuất hiện tại một frame, nhưng chưa biết
đó có phải cùng một phương tiện qua nhiều frame hay không. Tracker NvDCF bổ sung ID
theo thời gian, giúp tạo trajectory. Trajectory là điều kiện cần để ước lượng tốc độ,
lọc nhiễu và tránh đếm lặp một xe nhiều lần.

Trong bài toán điều phối, detector và tracker còn tạo ra tín hiệu workload. Số track
nhiều hơn thường kéo theo nhiều metadata, nhiều crop và nhiều xử lý biển số. Tuy nhiên
số track không được dùng trực tiếp để quyết định quá tải node nếu FPS vẫn tốt. Nó chỉ
là tín hiệu camera-level để chọn camera/crop phù hợp sau khi node-level load score đã
cho thấy cần can thiệp.

1.4.2. Nhận dạng biển số và crop offload

LPD và LPR là hai stage xử lý biển số. LPD xác định vùng biển số trong ảnh phương
tiện; LPR đọc ký tự từ vùng biển số đó. Hai stage này có thể được offload theo dạng
crop: node nguồn gửi plate crop hoặc vehicle crop sang peer để peer xử lý tiếp. Đây là
ý tưởng của L3/L2 offload.

Crop offload khác full stream migration. Với L3/L2, source node vẫn tiếp tục decode và
xử lý frame gốc; do đó crop offload không được trình bày như cơ chế chắc chắn giảm FPS
load của source theo cùng cách L1 làm. Nó phù hợp để chia bớt một phần công việc nhận
dạng, nhưng chỉ L1 full stream migration mới thật sự remove camera khỏi pipeline source
node. Sự phân biệt này quan trọng để tránh claim sai về tác động của L2/L3.

1.4.3. Homography, ROI và đo tốc độ

Homography biến đổi tọa độ ảnh sang mặt phẳng thực tế đã hiệu chỉnh. Với camera giao
thông cố định, người triển khai chọn bốn điểm nguồn trên ảnh và ánh xạ sang kích thước
thực như 15m x 60m. Khi tracker có trajectory trong tọa độ ảnh, SpeedProbe có thể biến
đổi các điểm sang tọa độ thế giới và ước lượng tốc độ. ROI polygon giúp giới hạn vùng
quan tâm, tránh xử lý các đối tượng ngoài làn đường hoặc vùng không có ý nghĩa đo tốc
độ.

Điểm liên hệ với hệ thống là cấu hình camera không chỉ chứa URI. Nó chứa source_id,
homography, ROI, speed limit, FPS và output config. Khi migration camera sang node
khác, camera_config phải đi cùng camera. Nếu chỉ gửi URI mà không gửi homography/ROI,
target có thể phát video nhưng không thể xử lý đúng nghiệp vụ giao thông.

1.4.4. Vì sao FPS là tín hiệu QoS chính

FPS là tín hiệu trực tiếp nhất phản ánh pipeline có đang cung cấp đủ dữ liệu thời gian
thực hay không. Trong hệ thống hiện tại, FPS được tính từ frame counter theo window
thực tế, có `session_id` và `sequence`, nên có thể audit được. CPU, RAM, nhiệt độ và
GPU% vẫn có giá trị chẩn đoán, nhưng chúng không thay thế được FPS. Một pipeline có
GPU% thấp nhưng FPS giảm vẫn không đạt mục tiêu; một pipeline có GPU% cao do burst
nhưng FPS ổn định chưa chắc cần offload.

Vì vậy load score được thiết kế theo nguyên tắc FPS-first. Workload và thermal chỉ là
bonus cộng dồn để score nhạy hơn trước một số dấu hiệu áp lực, nhưng chúng không tự
kích hoạt các mức offload nếu FPS chưa cho thấy QoS suy giảm. Đây là lựa chọn phù hợp
với mục tiêu thực tế của hệ thống: giữ giám sát liên tục, không tối ưu một metric phần
cứng đơn lẻ.

1.5. Kết luận chương 1

Chương 1 đã trình bày nền tảng lý thuyết: Jetson + DeepStream làm nền tảng
tính toán tại biên, Zenoh peer mode cung cấp giao tiếp P2P không cần broker,
và FPS là tín hiệu chính cho quyết định tải. Chương 2 sẽ trình bày thiết kế chi
tiết hệ thống.

-------------------------------------------------------------------------------------
CHƯƠNG 2: PHÂN TÍCH VÀ THIẾT KẾ GIẢI PHÁP MULTI-EDGE COORDINATION

2.1. Phân tích yêu cầu hệ thống

Giải pháp Multi-Edge Coordination được thiết kế theo chuỗi logic: bài toán vận hành
được chuyển thành yêu cầu, yêu cầu dẫn tới kiến trúc, kiến trúc sinh ra telemetry,
telemetry được tổng hợp thành load score, load score kích hoạt state machine, và state
machine tạo ra các hành động offload, migration, reclaim hoặc failover. Cách tổ chức
này giữ mối quan hệ nhân quả giữa vấn đề và cơ chế thay vì mô tả các module như những
khối độc lập.

2.1.1. Xác định bài toán, đầu vào và đầu ra

Bài toán được xét trong luận văn là điều phối nhiều Edge node xử lý camera giao thông
trong cùng một cụm mạng cục bộ. Đầu vào của hệ thống gồm các luồng RTSP từ camera,
cấu hình camera, cấu hình node, telemetry phần cứng, metadata sinh ra từ pipeline và
trạng thái heartbeat của các peer. Đầu ra của hệ thống gồm luồng RTSP đã xử lý cho
dashboard, health payload cho Server, heartbeat P2P cho các peer, quyết định offload
hoặc migration, CSV telemetry phục vụ đánh giá và các sự kiện nghiệp vụ như vi phạm
tốc độ khi path đó được kiểm chứng đầy đủ.

Giả định triển khai là các Edge node nằm trong cùng LAN hoặc một miền mạng có thể
trao đổi Zenoh; mỗi node có danh tính `NODE_ID`; mỗi camera có `camera_id` ổn định;
và camera_config đủ thông tin để node khác có thể mở lại cùng camera khi migration
hoặc rescue. Hệ thống không giả định có central controller. Server có thể offline mà
không làm policy P2P dừng, mặc dù dashboard khi đó sẽ mất khả năng quan sát.

Mục tiêu trực tiếp là duy trì QoS của pipeline, trong đó FPS là tín hiệu chính. Hệ
thống không tối ưu số lượng camera trên mỗi node theo kiểu chia đều tuyệt đối, vì hai
camera có thể tạo workload rất khác nhau. Hệ thống cũng không tối ưu GPU% vì GPU% trên
Jetson không đủ đáng tin làm trigger độc lập. Mục tiêu đúng hơn là: khi một node có
dấu hiệu QoS suy giảm kéo dài, nó tìm cách giảm áp lực hoặc chuyển camera bằng cơ chế
có kiểm soát, đồng thời tránh tạo dao động hoặc mất trạng thái camera.

2.1.2. Các ràng buộc thiết kế

Thiết kế chịu năm ràng buộc chính. Ràng buộc thứ nhất là runtime phải hoạt động liên
tục. Một quyết định offload không được làm pipeline crash hoặc dừng toàn bộ luồng.
Ràng buộc thứ hai là telemetry phải có tính truy vết. Không thể dùng một dòng CSV nếu
không biết nó thuộc session nào, sequence nào và window nào. Ràng buộc thứ ba là
policy phải phân tán. Node tự ra quyết định từ trạng thái local và heartbeat peer, vì
Server chỉ là observer. Ràng buộc thứ tư là quyết định phải có memory theo thời gian,
không phản ứng với spike một giây. Ràng buộc thứ năm là mọi claim về migration/failover
phải được kiểm chứng ở phía nhận, vì producer log không chứng minh hệ thống đã thật sự
nhận và hiển thị kết quả.

2.1.3. Yêu cầu chức năng

Hệ thống phải đáp ứng các yêu cầu sau:
a) Tiếp nhận và xử lý đa luồng video trên mỗi Edge node:
   - Mỗi thiết bị Edge (Jetson) xử lý đồng thời 2 đến 8 luồng RTSP.
   - Mỗi luồng gán một camera_id và cấu hình riêng (URI, homography, ROI polygon,
     speed_limit, FPS).
   - Hỗ trợ hot-reload cấu hình cameras.yml khi file thay đổi.

b) Phát hiện, theo dõi phương tiện và đo tốc độ:
   - YOLO detector (PGIE) phát hiện xe trong từng frame.
   - NvDCF tracker gán unique ID, duy trì trajectory.
   - Ma trận Homography biến đổi pixel → mét.
   - Tốc độ được smoothing bằng median filter, validation, phát hiện vượt tốc độ.
   - Snapshot + metadata khi phát hiện vi phạm.

c) Peer Orchestration — Cân bằng tải P2P:
   - Mỗi node chạy một PeerOrchestrator instance độc lập.
   - Heartbeat chứa: load_score, CPU%, RAM%, temp, FPS per camera, active cameras.
   - Peer state được cập nhật khi nhận thông tin từ các peer khác.
   - Khi quá tải (load_score vượt threshold trong thời gian xác định), kích
     hoạt cơ chế offload.
   - Sau khi chuyển giao thành công, peer tiếp nhận thực hiện migration.

d) Leaderless Failover:
   - Khi peer không gửi heartbeat trong heartbeat_timeout_s (5s), các peer sống
     đồng thời tính toán consistent hash để chọn rescuer.
   - Rescuer xác minh khả năng truy cập RTSP và thực hiện ADD camera.
   - Camera được cứu sẽ được theo dõi và tự động trả về owner khi trở lại online.

e) Multi-Level Offload:
   - Level 3 (load_score ≥ 57, ~22 FPS): gửi plate crops → peer chạy LPR.
   - Level 2 (load_score ≥ 65, ~19 FPS): gửi vehicle crops → peer chạy LPD + LPR.
   - Level 1 (load_score ≥ 75, ~17 FPS): full stream migration.
   - Tự động tăng dần (escalation) khi tải tăng.

f) Server giám sát (Server/):
   - Nhận health updates và violations từ các Edge node qua WebSocket.
   - Lưu trữ violations dưới dạng JSONL (theo ngày/node_id).
   - Push live updates đến browser dashboard.
   - Phục vụ hình ảnh snapshot.

g) Dashboard trực quan:
   - Hiển thị video grid.
   - Hiển thị trạng thái cluster (GPU%, CPU%, load score per node).
   - Nguồn cấp dữ liệu vi phạm theo thời gian thực.

Các yêu cầu chức năng trên được nhóm theo chuỗi xử lý. Nhóm xử lý video biến camera
thành metadata và output stream. Nhóm telemetry biến trạng thái pipeline thành dữ liệu
có thể kiểm chứng. Nhóm P2P biến dữ liệu đó thành quyết định phân tán. Nhóm Server và
dashboard biến health/state thành thông tin quan sát cho người vận hành. Việc tách
nhóm như vậy giúp tránh nhầm lẫn giữa control plane và observer plane: Server hiển thị
trạng thái, nhưng không chọn camera để migration.

2.1.4. Yêu cầu không chức năng

a) Độ chính xác: sai số tốc độ ≤ ±5%, ID switch ≤ 5% trong giao thông bình thường.
b) Hiệu năng thời gian thực: xử lý ≥ 25 FPS/camera, probe latency < 0.1ms.
c) Fault tolerance: migration theo Make-Before-Break để giảm rủi ro mất khung,
   failover tự động, tiếp tục hoạt động degraded khi mọi tier bị bão hòa. Số
   khung mất thực tế phải đo bằng receiving-side evidence trên Jetson thật.
d) Khả năng mở rộng: thêm/bớt Edge node qua Zenoh multicast, thêm/bớt camera
   qua hot-reload cameras.yml không restart pipeline.

Các yêu cầu không chức năng này có ảnh hưởng trực tiếp đến thiết kế. Độ chính xác đo
tốc độ phụ thuộc vào tracker, homography và FPS ổn định; do đó load score phải bảo vệ
FPS. Khả năng mở rộng phụ thuộc vào việc camera_config đi kèm heartbeat/control
message; nếu node nhận không có config camera, nó không thể rescue/migrate đúng. Fault
tolerance phụ thuộc vào Make-Before-Break và timeout thống nhất; nếu Edge dùng 5s mà
Server hoặc peer dùng giá trị khác, dashboard và policy sẽ nhìn hệ thống theo hai
trạng thái khác nhau.

2.1.5. Tiêu chí đánh giá thành công

Một hệ thống Multi-Edge không thể được đánh giá chỉ bằng câu "pipeline chạy". Luận văn
xác định bốn nhóm bằng chứng cần có. Nhóm thứ nhất là bằng chứng runtime: pipeline vào
PLAYING, snapshot sequence tăng, health cycle chạy đều. Nhóm thứ hai là bằng chứng
receiving-side: Server hoặc dashboard nhận health update, target nhận ADD và PLAYING
ack, survivor thật sự ADD orphan camera khi peer chết. Nhóm thứ ba là bằng chứng dữ
liệu: CSV collection không stale, không duplicate, không trộn session. Nhóm thứ tư là
bằng chứng hồi quy: unit/focused regression tests bảo vệ các logic không phụ thuộc
Jetson như timeout, state machine và parser.

Các bằng chứng này có độ mạnh khác nhau. Unit test giúp phát hiện lỗi logic nhanh,
nhưng không chứng minh TensorRT engine load được trên Jetson. Producer log cho biết
node đã thử gửi message, nhưng không chứng minh peer hoặc Server nhận được. Receiving
side evidence mạnh hơn vì nó xác nhận luồng dữ liệu đã đi qua mạng và được consumer xử
lý. Vì vậy, những claim vận hành quan trọng trong luận văn đều được gắn với loại bằng
chứng tương ứng.

2.2. Thiết kế kiến trúc hệ thống

2.2.1. Sơ đồ kiến trúc tổng thể

Kiến trúc được chia thành 4 tầng (Hình 2.1):
1. Tầng Camera: các nguồn RTSP và cấu hình camera.
2. Tầng Edge: pipeline DeepStream, SpeedProbe, camera manager, health publisher,
   và coordination.
3. Tầng P2P: trạng thái Zenoh peer, offload commands, migration/reclaim,
   peer-offline rescue.
4. Tầng giám sát: aiohttp Server, violation storage, WebSocket forwarding,
   MediaMTX, dashboard browser.

Server nằm ngoài vòng lặp điều khiển — quan sát mà không quyết định.

Lựa chọn này xuất phát từ mục tiêu tránh single point of failure trong control plane.
Nếu Server ra quyết định migration, toàn bộ cụm Edge sẽ phụ thuộc vào Server và đường
truyền đến Server. Khi Server lỗi, dashboard mất quan sát là điều có thể chấp nhận
tạm thời, nhưng camera coordination không nên dừng. Vì vậy kiến trúc đặt policy trong
PeerOrchestrator ở từng node. Server chỉ nhận health, lưu registry và broadcast cho
browser.

2.2.1.1. Vai trò của từng tầng trong lập luận hệ thống

Tầng Camera cung cấp nguồn dữ liệu vật lý và các thông số hiệu chỉnh. Đây là nơi ràng
buộc thực tế xuất hiện: URI có thể mất kết nối, ROI/homography phải đúng với góc đặt
camera, FPS source có thể thấp hơn kỳ vọng. Tầng Edge xử lý dữ liệu thô thành stream
và telemetry. Đây là nơi quyết định chất lượng dịch vụ được đo. Tầng P2P sử dụng
telemetry để phối hợp giữa các Edge node. Đây là nơi policy phân tán hoạt động. Tầng
giám sát hiển thị cho người vận hành và lưu một phần dữ liệu, nhưng không điều khiển
policy.

Sự phân tầng này giúp hệ thống có khả năng suy giảm có kiểm soát. Nếu dashboard lỗi,
Edge vẫn xử lý camera. Nếu một peer lỗi, các peer còn lại có thể rescue orphan camera.
Nếu một camera source yếu, source-starved detection giúp không chọn sai camera đó làm
candidate offload. Nếu telemetry invalid, load score trả về worst-case thay vì giả vờ
healthy.

2.2.2. Zenoh Key Expressions

| Key Expression | Publisher | Payload | Semantics |
|---|---|---|---|
| peers/status/{node_id} | HealthAgent | msgpack: GPU%, CPU%, RAM%, load_score, FPS, active_cameras | Heartbeat mỗi 1s |
| peers/vote/request | PeerOrch | msgpack: requester, camera_id, load_score, eps | Mở cửa sổ chọn peer nhận camera |
| peers/vote/proposal | PeerOrch | msgpack: bidder, camera_id, score, fps_predicted, rtt_ms | Peer đề xuất nhận camera nếu thỏa điều kiện |
| peers/vote/decision | PeerOrch | msgpack: winner, camera_id, cam_config | Kết quả chọn peer nhận |
| peers/vote/ack/{camera_id} | PeerOrch | msgpack: node_id, camera_id, event: PLAYING | Stream ready |
| peers/control/{node_id} | PeerOrch | msgpack: cmd ADD/REMOVE, camera_config | Control command |
| traffic/events/{node_id}/{camera_id} | SpeedProbe | msgpack: overspeed event + snapshot | Violation |
| offload/plates/{src}/{dst} | OffloadPublisher | msgpack: plate crop JPEGs | Level 3 offload |
| offload/vehicles/{src}/{dst} | OffloadPublisher | msgpack: vehicle crop JPEGs | Level 2 offload |
| offload/results/{node_id}/{sender} | OffloadReceiver | msgpack: decoded result | Offload result |

Bảng 2.1. Zenoh Key Expressions

Các key expression trên được chia thành ba loại. Loại thứ nhất là status path
`peers/status/{node_id}`, được publish đều đặn để peer biết trạng thái nhau. Loại thứ
hai là control path, gồm vote/control/ack, dùng cho migration và reclaim. Loại thứ ba
là data offload path, gồm plate/vehicle crop và result. Việc tách key như vậy giúp
policy không trộn health với payload crop, đồng thời giúp debug dễ hơn vì mỗi nhóm có
ý nghĩa vận hành riêng.

Trong runtime hiện tại, cần lưu ý rằng tên `vote` trong key không có nghĩa hệ thống
đang triển khai một thuật toán voting phức tạp. Luận văn nên hiểu nó như cửa sổ chọn
peer nhận camera hoặc nhận crop dựa trên trạng thái hiện có. Policy thực tế được quyết
định bởi PeerOrchestrator và các guard như capacity, penalty, thermal, cooldown, dwell
time và workload evidence.

2.2.3. Luồng dữ liệu trong pipeline DeepStream

(1) Nguồn video: N camera RTSP → uridecodebin → giải mã hardware.
(2) Ghép batch: nvstreammux gom N luồng thành batch, size=N.
(3) Primary Inference (PGIE): YOLO + TensorRT → bounding boxes + labels.
(4) Tracker (NvDCF): gán unique ID, duy trì trajectory.
(5) Secondary Inference (LPD + LPR): phát hiện + nhận dạng biển số.
(6) Analytics (nvdsanalytics): ROI filter, cung cấp pad cho SpeedProbe.
(7.1) ROIFilterProbe: lọc vehicle ngoài ROI polygon.
(7.2) SpeedProbe: homography transform, tính tốc độ, validation, publishing.
(8) OSD (nvdsosd): vẽ bbox, ID, speed, plate text.
(9) Tiling (nvmultistreamtiler): ghép N camera thành grid.
(10) Sink: rtspclientsink → MediaMTX, hoặc display, hoặc file.

Hình 2.2. Pipeline DeepStream với các probes

Luồng dữ liệu này thể hiện vì sao telemetry nên được lấy từ trong pipeline thay vì chỉ
từ hệ điều hành. CPU/GPU/RAM cho biết trạng thái phần cứng, nhưng không cho biết từng
camera đang có bao nhiêu frame, bao nhiêu track hoặc output FPS nào. SpeedProbe ở trong
pipeline nhìn thấy metadata sau inference/tracking và có thể gắn trạng thái đó với
camera_id. Nhờ vậy coordination có dữ liệu node-level và camera-level cùng lúc.

2.2.4. Mô hình triển khai phần cứng

Hệ thống gồm 4 thiết bị Jetson Edge kết nối qua mạng LAN (gigabit switch). Mỗi
Jetson có 2 camera IP (RTSP), MediaMTX relay nhận composite RTSP, và một
Central Server (aiohttp) nhận WebSocket health/violations từ HealthAgent.
Browser kết nối Server WebSocket + MediaMTX để xem dashboard.

Trong triển khai thực tế, mỗi Jetson phải có cấu hình riêng cho `NODE_ID`, địa chỉ
RTSP source, `MONITOR_URL`, `RTSP_PUSH_URL` và thông số camera vật lý. Không nên coi
file config của Jetson A là config chung cho mọi node. Một node nhận camera từ node
khác cần camera_config đi kèm heartbeat/control message để biết URI, source_id,
homography và ROI. Đây là điều kiện để migration không chỉ phát được stream, mà còn
giữ đúng logic xử lý giao thông.

2.3. Thiết kế hệ thống telemetry và load score

2.3.1. Unified telemetry snapshot

SpeedProbe writes một JSON snapshot đồng thời (atomic). Snapshot bao gồm:

| Trường | Mô tả |
|---|---|
| _telemetry.session_id | Pipeline session identity; thay đổi khi restart |
| _telemetry.sequence | Số snapshot tăng dần trong session |
| _updated_at | Timestamp freshness |
| camera keys | Output FPS per camera |
| _input_fps | Input FPS khi có |
| _features | Per-camera stats (n_track, n_plate, stationary_fraction) |
| _offload_crops | Crop receive rate (khi bật) |
| load_score_breakdown | Các thành phần dùng cho quyết định health |

Bảng 2.2. Các trường telemetry chính

Mọi reader (HealthAgent, profile_collect, PeerOrchestrator) phải kiểm tra
freshness và sequence advancement trước khi sử dụng snapshot. Điều này ngăn
vi phạm đa consumer xử lý cùng một snapshot cũ như snapshot mới. Cadence
health được cấu hình 1 giây, khớp với writer.

Thiết kế snapshot thống nhất giải quyết một lỗi thường gặp trong hệ thống realtime:
mỗi consumer tự đọc một nguồn khác nhau, rồi ghép các giá trị không cùng thời điểm.
Nếu HealthAgent đọc FPS từ một file, profile_collect đọc feature từ nguồn khác và
PeerOrchestrator tự tính trạng thái riêng, các quyết định có thể dựa trên những lát
cắt thời gian không khớp. Unified snapshot làm giảm lỗi đó bằng cách tạo một payload
đại diện cho một window pipeline cụ thể. Payload này không chỉ chứa giá trị, mà còn
chứa identity của window.

`session_id` giải quyết vấn đề pipeline restart. Khi pipeline restart, sequence có thể
bắt đầu lại từ 0; nếu không có session_id, collector có thể hiểu nhầm sequence mới là
duplicate hoặc nối nhầm hai phiên chạy. `sequence` giải quyết vấn đề duplicate/stale.
Nếu reader đọc cùng một snapshot hai lần, sequence không tăng và row bị bỏ. Window
start/end monotonic giải quyết vấn đề cadence không hoàn hảo. Nếu thread bị trễ, FPS
vẫn được tính theo duration thực thay vì giả định đúng 1 giây.

2.3.2. Load score thiết kế

Load score là composite. Đường cơ sở FPS anchor vẫn là lõi, và các tín hiệu
workload (n_track + n_plate) cùng thermal (gpu_temp_c) góp bonus cộng dồn
để điểm tăng trước khi FPS giảm. Load score = min(100, fps_score +
workload_bonus + thermal_bonus). CPU/RAM fuse applied sau theo config.

Ở chế độ hiện tại (reactive baseline):

| Mức | Load score | FPS anchor | Hành động |
|---|---|---:|---|
| L3 | 57 | ~22 | crop offload (plate) |
| L2 | 65 | ~19 | crop offload (vehicle) |
| L1 | 75 | ~17 | full stream migration |
| Reclaim | < 50 (57 - margin 7) | | reclaim camera |

Bảng 1.2. Ngưỡng load score phản ứng

Đây là chính sách hiện tại, KHÔNG phải công thức dự báo học sâu. Proactive mode
= disabled trong runtime hiện tại.

2.3.3. Ý nghĩa của các anchor FPS

Các anchor 27, 22, 19 và 17 FPS không chỉ là con số tùy ý. Chúng ánh xạ trạng thái QoS
sang mức can thiệp. Ở vùng gần 27 FPS, pipeline được coi là healthy và load score thấp.
Khi FPS giảm về khoảng 22, hệ thống bắt đầu coi đây là suy giảm nhẹ và có thể thử L3.
Khoảng 19 FPS thể hiện suy giảm rõ hơn, tương ứng L2. Khoảng 17 FPS là vùng nghiêm
trọng hơn, tương ứng L1 full stream migration. Việc dùng đường cong liên tục thay vì
if/else rời rạc giúp score thay đổi mượt hơn và dễ hiển thị trên dashboard.

Workload bonus và thermal bonus được cộng sau FPS score để phản ánh áp lực sớm. Nếu
một node có nhiều track/plate hoặc nhiệt độ GPU tăng, score có thể tăng thêm, nhưng
bonus bị giới hạn để không lấn át FPS. Đây là cách cân bằng giữa phản ứng sớm và tránh
false trigger. CPU/RAM emergency floor chỉ kích hoạt khi tài nguyên hệ thống rất cao
và FPS đã giảm dưới ngưỡng gần mục tiêu, nhằm tránh trường hợp CPU/RAM đầy làm dịch vụ
khác chết trước khi FPS anchor phản ứng đủ.

2.3.4. Vì sao không dùng GPU utilization làm trigger

GPU utilization dễ hấp dẫn vì nó có vẻ là tín hiệu phần cứng trực tiếp. Tuy nhiên trên
Jetson, cách đo GPU% không giống dGPU server. TensorRT có thể chạy theo burst ngắn;
GR3D sample theo chu kỳ có thể bắt trúng hoặc bỏ lỡ burst; DVFS làm cùng một workload
có thể tạo phần trăm khác nhau tùy clock; một số phần của video pipeline như decode
hoặc VIC không được phản ánh đầy đủ bởi GPU%. Do đó GPU% có thể cao khi FPS vẫn tốt,
hoặc thấp khi pipeline vẫn drop frame do bottleneck khác.

Vì lý do đó, hệ thống vẫn publish GPU% để quan sát và phân tích, nhưng không dùng nó
làm trigger độc lập. Đây là một quyết định thiết kế quan trọng: mục tiêu là QoS video,
không phải giữ GPU% dưới một con số đẹp.

2.4. Máy trạng và cơ chế điều phối

2.4.1. Decision loop

PeerOrchestrator chạy một vòng lặp 1 giây độc lập trên mỗi node. Vòng lặp
kiểm tra: peer offline, rebalance (trả camera được cứu), reclaim, và self-overload.

Thứ tự kiểm tra thể hiện ưu tiên vận hành. Peer offline được kiểm tra trước vì mất
node có thể tạo camera orphan cần cứu ngay. Rebalance/reclaim được kiểm tra trước
self-overload để hệ thống có cơ hội trả camera về owner khi trạng thái đã ổn. Self-
overload xử lý sau cùng để tránh vừa cứu/thu hồi vừa tiếp tục đẩy thêm camera ra trong
cùng một chu kỳ không cần thiết. Đây là một state machine có ưu tiên, không phải tập
hợp các if độc lập.

Mỗi peer được biểu diễn bằng PeerState gồm load_score, FPS trung bình, FPS per camera,
active cameras, camera configs, max streams, last_seen, penalty, workload per camera
và source-starved cameras. State này đủ để node tự đánh giá peer nào còn sống, peer
nào có capacity, camera nào có thể nhận và camera nào không nên chọn.

2.4.2. Camera selection

Camera được chọn theo workload (pipeline.camera_workload = n_track + n_plate),
không theo FPS output. L1 chọn camera có workload thấp nhất, L2/L3 chọn
camera có workload cao nhất. Source-starved và reclaim-bubble cameras được
loại trừ. Candidate không có workload evidence trả về None (fail-safe).

Lựa chọn này giải quyết hai lỗi logic. Lỗi thứ nhất là chọn camera có FPS thấp nhất.
Camera đó có thể bị source-starved do RTSP input yếu, nên chuyển nó đi không làm giảm
tải cho source node và còn có thể làm peer nhận một nguồn xấu. Lỗi thứ hai là chọn
camera workload cao nhất cho L1. Full-stream migration chuyển toàn bộ camera sang peer;
nếu chọn camera quá nặng, peer nhận có thể lập tức quá tải. Vì vậy L1 chọn workload
thấp nhất để giảm số source trên node nguồn với rủi ro thấp hơn, còn L2/L3 chọn
workload cao nhất vì crop offload chỉ có ý nghĩa khi camera đó sinh nhiều crop.

2.4.3. Make-Before-Break migration

1. WINNER nhận ADD → bắt đầu stream camera từ RTSP source → đợi PLAYING.
2. WINNER gửi peers/vote/ack/{camera_id} (payload: event: PLAYING).
3. REQUESTER nhận ack (timeout 15s) → gửi REMOVE.
4. Nếu timeout → rollback, penalize winner trong cooldown.

REQUESTER không bao giờ dừng stream trước khi WINNER đã confirm. Stream mới
bắt đầu → confirm → stream cũ dừng. Thiết kế này giảm rủi ro mất khung do dừng
nguồn quá sớm; số frame mất thực tế vẫn phải đo ở output receiving side.

Make-Before-Break là cơ chế bắt buộc vì camera stream là tài nguyên liên tục. Nếu
source remove trước rồi target add sau, bất kỳ lỗi nào ở target sẽ tạo khoảng trống
giám sát. Ngược lại, nếu target add trước và chỉ ack khi PLAYING, source có bằng chứng
tối thiểu rằng camera đã tồn tại ở phía nhận. Cơ chế này không loại bỏ mọi frame loss
vì còn network, muxer, encoder và player, nhưng loại bỏ một lớp lỗi thiết kế nghiêm
trọng: dừng nguồn trước khi đích sẵn sàng.

2.4.4. Leaderless failover với consistent hashing

Khi một peer không heartbeat trong 5s, các peer sống đồng thời phát hiện.
Với mỗi camera orphaned, tính hash(SHA-256) → chọn rescuer theo index = hash %
số peer. Tất cả peer cùng thuật toán → cùng kết quả → không cần election. Sau
jitter 0–3s, rescuer verify RTSP → ADD camera. Camera được cứu sẽ được trả
về owner khi online trở lại.

Consistent hashing được dùng để tránh cần leader election. Nếu mọi survivor nhìn thấy
cùng danh sách peer sống và cùng danh sách orphan camera, cùng thuật toán hash sẽ chọn
cùng rescuer. Jitter 0-3 giây làm giảm rủi ro trường hợp view tạm thời lệch khiến hai
node cùng thử ADD. Trong hệ thống giao thông, rescue orphan stream là yêu cầu
availability, không phải tối ưu phụ. Khi một Jetson chết, camera của nó không nên biến
mất khỏi toàn hệ nếu peer khác còn khả năng mở RTSP source.

2.4.5. Camera reclaim (tự phục hồi)

Khi load giảm (load < reclaim_threshold = 50) trong reclaim_stable_s = 15s,
node gửi ADD để reclaim camera đã migrate. Make-Before-Break cho reclaim:
ADD camera về self → đợi PLAYING ack → REMOVE đến holder node.

Reclaim giúp hệ thống trở lại trạng thái ownership ban đầu sau khi tải giảm hoặc node
offline quay lại. Nếu không reclaim, một camera có thể bị giữ lâu dài trên peer khác
dù owner đã khỏe lại. Tuy nhiên reclaim cũng phải có stability window để tránh ping-
pong. Chỉ khi load thấp ổn định trong một khoảng thời gian, node mới thử lấy lại
camera. Sau reclaim, camera đi vào reclaim bubble để không bị offload lại ngay.

2.5. Server giám sát và Dashboard

2.5.1. Central Server (aiohttp trên VPS)

Server chạy trên VPS, không phải Edge node. Cấu tạo:
- /ws/edge: WebSocket cho Edge nodes. Forward health + violations đến browser.
- /ws/server: WebSocket cho browsers. Phát health + violations + edge_offline.
- /api/edges: danh sách edges + live health.
- /api/clusters: group edges theo subnet.
- /api/violations: query violations từ JSONL store.
- /api/streams: proxy tới MediaMTX API.
- /api/snapshots/{node}/{file}: serve snapshot images.
- /health: health check endpoint.
- ViolationStore: async write JSONL, lưu trữ theo ngày/node_id.
- EdgeRegistry: in-memory state, watchdog kiểm tra 5s, timeout 5s (đã điều
  chỉnh đồng bộ với Edge), mark offline → broadcast edge_offline.

Server được thiết kế như observer path. Điều này có hai ý nghĩa. Thứ nhất, Server là
nơi thuận tiện để người vận hành xem trạng thái cluster, nhưng không phải dependency
cho P2P decision. Thứ hai, mọi bằng chứng dashboard phải đi qua Server receiving path.
Nếu Edge log báo đã gửi health nhưng `/ws/server` hoặc browser không nhận update, thì
đường quan sát vẫn chưa đạt. Luận văn vì vậy tách rõ producer-side evidence và
receiving-side evidence.

Hình 2.4. Dashboard Cluster Status

2.5.2. Dashboard (static/index.html)

Single-page application:
- Video Panel: nhận video grid.
- Cluster Status Panel: real-time edge cards (node_id, IP, online status, GPU/CPU/
  RAM/Temp/Load Score/FPS), cập nhật qua WebSocket DOM diffing.
- Violation Feed Panel: violations newest-first, filter theo node_id/ngày, thumbnail.

Dashboard phục vụ hai mục đích. Mục đích vận hành là giúp người dùng nhìn nhanh node
nào online, node nào quá tải, camera nào đang active và stream nào đang hiển thị. Mục
đích kiểm chứng là cung cấp bằng chứng phía nhận cho health update và trạng thái
offline. Dashboard không ra quyết định migration và không tính load score; các quyết
định này thuộc về Edge node.

2.5.3. Watchdog heartbeat timeout

Server và Edge đồng bộ heartbeat_timeout_s = 5.0s:
- Edge: config trong Edge/configs/edge_node.yml.
- Server: hằng số HEARTBEAT_TIMEOUT trong Server/edge_registry.py.
- README: ghi nhận cả hai đều dùng 5s.
- Server WebSocket heartbeat cũng dùng HEARTBEAT_TIMEOUT này.

2.6. Kết luận chương 2

Chương 2 đã hoàn thành thiết kế chi tiết. Kiến trúc hoàn toàn phân tán —
không có coordinator trung tâm. Telemetry contract đảm bảo tính toàn vẹn dữ
liệu. Các giá trị timeout được chuẩn hóa thành 5s duy nhất nguồn trên Edge
và Server. Chương 3 sẽ trình bày triển khai và kết quả đánh giá.

Từ góc nhìn lập luận, Chương 2 đã chuyển bài toán từ nhu cầu chung thành một giải
pháp cụ thể. Hệ thống đo QoS bằng FPS-first load score, bảo vệ dữ liệu bằng unified
snapshot, điều phối bằng PeerOrchestrator phân tán, và quan sát bằng Server/dashboard.
Những lựa chọn này chuẩn bị cho Chương 3, nơi mỗi thiết kế phải được đối chiếu với
module triển khai và bằng chứng kiểm chứng tương ứng.

-------------------------------------------------------------------------------------
CHƯƠNG 3: XÂY DỰNG, PHÁT TRIỂN VÀ ĐÁNH GIÁ HỆ THỐNG

Hệ thống được hiện thực thành các nhóm chức năng: Edge runtime, DeepStream pipeline,
SpeedProbe telemetry, HealthAgent publisher, profile collector, PeerOrchestrator P2P,
Server registry và dashboard. Mỗi nhóm có mục đích, đầu vào, xử lý và đầu ra riêng,
nhưng cùng phục vụ một dòng dữ liệu chung từ camera RTSP đến telemetry, health,
decision, migration và bằng chứng đánh giá.

Phần đánh giá được tách khỏi phần xây dựng. Trước hết báo cáo mô tả hệ thống được tạo
ra như thế nào, sau đó xác định phương pháp thực nghiệm, cuối cùng mới trình bày kết
quả và ranh giới kết luận. Những hành vi chưa có bằng chứng Jetson end-to-end được xem
là giới hạn cần kiểm chứng tiếp, không đưa vào nhóm kết quả đã đạt.

3.1. Triển khai Edge runtime và pipeline DeepStream

3.1.1. Pipeline động và lifecycle

run_edge.sh là entry point vận hành. main.py khởi động chế độ runtime được
chọn, DeepStream pipeline, health handling, và coordination components.
core_pipeline.py tạo đồ thị nguồn động. CameraManager ánh xạ camera_id ↔
source_id, thực hiện dynamic add/remove qua GLib.idle_add (thread-safe).

Về đầu vào, `run_edge.sh` nhận cấu hình từ biến môi trường, `Edge/.env`, tham số CLI
và hai file cấu hình chính: `configs/edge_node.yml` và `configs/cameras.yml`. Về đầu
ra, script tạo ba nhóm process khi chạy đầy đủ: `health_agent.py`, `main.py` chứa
pipeline, và `profile_collect.py` khi bật chế độ collect. Việc để `run_edge.sh` làm
entry point duy nhất giúp data collection bám đúng runtime thật. Nếu collector được
chạy riêng, nó có thể đọc snapshot cũ hoặc snapshot của pipeline khác mà không phản
ánh phiên vận hành đang cần đánh giá.

`main.py` có vai trò nhỏ nhưng quan trọng: nó gom CLI rồi chuyển quyền điều khiển cho
`run_python_mode`. Phần runtime thật nằm trong `speedflow_python/run_python.py`, nơi
CameraManager được tạo, edge_node.yml được đọc, PeerOrchestrator được khởi tạo, shared
Zenoh session được truyền cho offload publisher/receiver và pipeline DeepStream được
build. Cách tách này giữ entry point rõ ràng: shell script quản lý process, main.py
quản lý CLI, run_python.py quản lý lifecycle pipeline.

Pipeline `rtsp_push` chạy theo vòng restart có backoff. Nếu pipeline vào PLAYING, hệ
thống publish composite RTSP output về MediaMTX. Nếu nguồn RTSP lỗi, code cố gắng loại
riêng source lỗi thay vì giết toàn bộ pipeline. Nếu lỗi nghiêm trọng làm pipeline phải
restart, CameraManager không bị stop trong mọi finally block; watcher và processor
thread được giữ để hot-reload và dynamic ADD/REMOVE tiếp tục hoạt động sau restart.
Đây là chi tiết triển khai phản ánh yêu cầu runtime liên tục.

Hình 3.1. Cấu trúc dòng lệnh run_edge.sh

3.1.2. SpeedProbe — Core CV logic

SpeedProbe là GStreamer pad probe gắn vào sink pad của nvdsosd hoặc
nvmultistreamtiler. Quy trình mỗi frame:
1. Giải nén batch metadata từ gst_buffer.
2. Duyệt danh sách đối tượng → thu thập bounding boxes.
3. Tính toán tốc độ, validation, publishing vi phạm.
4. Xóa trạng thái cũ.

SpeedProbe là điểm nối giữa computer vision và coordination. Ở lớp nghiệp vụ giao
thông, nó đọc metadata object, camera_id, track ID, bbox, class và thông tin biển số để
tính trajectory, tốc độ, violation và overlay. Ở lớp coordination, nó đếm frame theo
camera, tổng hợp feature counts và tạo unified snapshot. Một module vừa phục vụ nghiệp
vụ vừa phục vụ telemetry như vậy cần contract rõ, vì nếu nó ghi snapshot sai, cả
HealthAgent, profile_collect và PeerOrchestrator đều sẽ bị ảnh hưởng.

Về đầu vào, SpeedProbe nhận metadata từ GStreamer buffer đã đi qua inference/tracking.
Về xử lý, nó cập nhật state theo camera và theo tracked object, áp dụng ROI/homography,
lọc track quá trẻ hoặc dữ liệu không hợp lệ, rồi cập nhật counter cho FPS và feature.
Về đầu ra, nó có thể publish violation event, cập nhật overlay, và quan trọng nhất đối
với luận văn là ghi `/dev/shm/speedflow_fps.json`. Snapshot này là cầu nối giữa pipeline
realtime và các module điều phối ngoài pipeline.

3.1.3. FPS writer lifecycle

SpeedProbe._fps_writer_loop:
- Ghi nhận window_started (monotonic).
- Chờ TELEMETRY_INTERVAL (1s).
- Tính window_dur = window_ended - window_started.
- Drain frame counters → FPS = frames / window_dur.
- Flush feature accumulators.
- Tăng sequence số.
- Build unified atomic payload (temp file + os.replace).
- stop_fps_writer() dừng thread an toàn.

Pipeline restart → session_id thay đổi → readers reset sequence tracking.

Điểm đáng chú ý là writer không ghi trực tiếp đè vào file đích. Nó build payload, ghi
vào file tạm rồi dùng `os.replace` để thay thế nguyên tử. Nhờ vậy reader không đọc
trúng JSON đang ghi dở. Đây là một quyết định nhỏ nhưng quan trọng trong hệ thống có
nhiều process đọc chung một file trong `/dev/shm`.

FPS được tính bằng `frames / window_duration_s`, không chia cứng cho một giây. Nếu
thread writer bị scheduler làm trễ, window duration sẽ phản ánh độ trễ đó. Cách này
giúp FPS trung thực hơn so với giả định cadence lý tưởng. Luận văn cần nhấn mạnh điểm
này vì FPS là load signal chính; nếu FPS calculation sai, load score và mọi quyết định
offload phía sau đều mất cơ sở.

3.2. Hệ thống thu thập telemetry và data contract

3.2.1. HealthAgent — validate trước publish

HealthAgent đọc một snapshot đã validate mỗi chu kỳ:
- _read_payload(): đọc file JSON atomic từ SpeedProbe.
- _validate_payload(): kiểm tra session_id, sequence, _updated_at freshness,
  sequence advancement. Từ chối stale/non-advancing.
- Tính load_score từ snapshot.
- Publish heartbeat lên Zenoh peers/status/{node_id} (msgpack).
- Forward tới Server qua WebSocket.

HealthAgent là publisher chính của trạng thái node ra bên ngoài. Nó không chỉ đọc
metric phần cứng từ jtop, mà còn reload edge_node.yml theo mtime, đọc snapshot pipeline,
detect source-starved cameras, derive camera workload, tính load score và publish cùng
một payload cho P2P/Server. Nếu jtop không mở được, hệ thống fallback hardware metrics
về 0 thay vì crash; load score vẫn có thể dựa vào FPS snapshot. Đây là lựa chọn phù hợp
với thiết kế FPS-first.

Source-starved detection là một guard quan trọng. Camera output FPS thấp không tự động
có nghĩa camera đó tạo quá tải. Nếu input FPS cũng thấp, nguyên nhân có thể là source
RTSP yếu. HealthAgent chỉ đánh dấu source-starved khi có đủ bằng chứng input-side. Peer
selection sau đó loại camera này khỏi candidate để tránh migrate một camera mà bản thân
nguồn đã không ổn định.

3.2.2. profile_collect — độc lập loại trùng/stale

profile_collect.py chạy trong run_edge workflow, không phải process riêng:
- Đọc cùng một snapshot.
- Độc lập verify session_id, sequence advancement (không tin snapshot đúng
  vừa đọc).
- Reject duplicate / stale row trước khi ghi CSV.
- Mỗi CSV row tự nhận diện (session_id/sequence/pipeline_window_*).

profile_collect có mục đích khác HealthAgent. HealthAgent phục vụ runtime decision và
monitoring; profile_collect phục vụ dữ liệu đánh giá. Vì vậy collector không được tin
rằng snapshot đã hợp lệ chỉ vì HealthAgent cũng đọc được nó. Collector tự kiểm tra
identity, freshness và sequence advancement. Trước khi commit một session, collector
đòi hỏi candidate session có sequence tăng trong warmup khoảng 6 giây. Sau khi commit,
session đổi hoặc sequence không tăng đều bị bỏ.

Đầu ra của collector là CSV có cả telemetry identity và feature aggregate: timestamp,
hardware metrics, session_id, sequence, window start/end/duration, fps_avg,
input_fps_avg, n_active_cameras, n_track_total, n_plate_total, stationary fraction,
offload receive rate, load_score và delta_load. Một dòng CSV vì vậy không chỉ là giá
trị đo, mà là một quan sát có thể truy ngược về snapshot pipeline cụ thể.

3.2.3. Dòng lệnh thu thập dữ liệu

Collection phải được nhúng trong run_edge workflow — không có separate collector
process, không cần startup thủ công.

Trong thực tế, command collect đúng phải bảo đảm pipeline thật đang chạy trước khi
collector ghi dữ liệu. `run_edge.sh --collect` chờ file snapshot xuất hiện rồi mới
start collector. Tuy nhiên cần ghi trung thực rằng script từng có default
`COLLECT_INTERVAL=2.0`, trong khi telemetry stack hiện khóa cadence 1.0. Vì vậy khi
thu thập dữ liệu mới, command nên truyền rõ `--collect-interval 1.0` hoặc đồng bộ lại
default script trước khi dùng kết quả làm bằng chứng. Đây là ví dụ cho thấy luận văn
không chỉ mô tả hệ thống tốt đẹp, mà còn chỉ ra contract vận hành cần giữ đúng.

Hình 3.2. Quy trình thu thập và kiểm tra dữ liệu

3.2.4. End-to-end collection verification

1. Start real Edge pipeline + embedded collection workflow.
2. Producer logs: pipeline running, snapshot advancing.
3. Server nhận được health payload.
4. Inspected CSV rows: sequence tăng dần, không trùng lặp, không stale.
Producer-side logs không đủ — receiving-side verification bắt buộc.

Lý do producer-side logs không đủ là vì pipeline realtime có nhiều điểm rơi: Edge có
thể ghi snapshot nhưng Server không nhận health; requester có thể gửi ADD nhưng target
không vào PLAYING; target có thể ack nhưng browser không update; collector có thể mở
file nhưng đọc lại cùng một sequence. Vì vậy, mỗi kịch bản kiểm chứng cần xác định rõ
điểm quan sát cuối cùng. Với collection, điểm quan sát là CSV sequence tăng và không
stale. Với monitoring, điểm quan sát là Server/browser nhận update. Với migration,
điểm quan sát là target stream ready trước source remove.

3.3. Triển khai PeerOrchestrator P2P

3.3.1. Zenoh session factory

Zenoh peer mode qua UDP multicast scouting — các Jetson tự discovery nhau
trong 1s. Shared session cho toàn bộ giao tiếp.

Việc dùng shared Zenoh session tránh mở nhiều kết nối riêng cho từng chức năng. Trong
runtime, PeerOrchestrator tạo hoặc nhận session, sau đó offload publisher và receiver
có thể dùng chung session này. Điều này giảm overhead và giữ đường truyền P2P nhất
quán. Mỗi message được encode bằng msgpack để payload nhỏ hơn JSON text trong đường
trao đổi lặp lại.

Session factory không ra policy. Nó chỉ cung cấp kênh truyền. Policy nằm ở các handler
và decision loop: status subscriber cập nhật PeerState, control subscriber xử lý ADD/
REMOVE, ack subscriber xác nhận migration, offload subscriber nhận crop. Tách transport
và policy giúp hệ thống dễ kiểm thử hơn vì một số logic state machine có thể test mà
không cần Jetson hoặc camera thật.

3.3.2. Heartbeat và peer state

HealthAgent publish heartbeat mỗi 1s (cấu hình HEALTH_INTERVAL=1.0s trong
Edge/.env), payload gồm: type, node_id, timestamp, load_score, GPU%, CPU%,
RAM%, gpu_temp_c, power_mw, omega_preset, fps_per_camera, active_cameras,
camera_configs. PeerOrchestrator subscribe peers/status/** → _on_peer_status()
→ cập nhật _peers[peer_id].

Heartbeat là nguồn sự thật phân tán cho peer state. Một heartbeat không chỉ nói node
còn sống, mà còn mang theo capacity và camera context. `camera_configs` cho phép peer
khác biết cách mở camera khi cần rescue hoặc migration. `camera_workload` giúp chọn
camera theo chi phí xử lý. `source_starved_cameras` giúp loại camera có nguồn yếu khỏi
candidate. `last_seen` giúp offline watchdog xác định node mất heartbeat.

Vì heartbeat được gửi mỗi giây và timeout là 5 giây, hệ thống có khoảng chịu lỗi ngắn
trước khi coi peer offline. Giá trị này thống nhất với Server registry để dashboard và
Edge policy nhìn cùng một trạng thái offline. Nếu hai bên lệch timeout, người vận hành
có thể thấy node vẫn online trong dashboard trong khi peer đã rescue camera, hoặc ngược
lại.

Bảng 3.1. Cấu hình runtime Edge hiện tại

| Cấu hình | Giá trị | Nguồn |
|---|---|---|
| LOAD_POLICY | actual | Edge/.env |
| LOAD_MODEL | formula | Edge/.env |
| proactive.enabled | false | configs/edge_node.yml |
| HEALTH_INTERVAL | 1.0 | Edge/.env |
| HEALTH_LOG_EVERY | 12 | Edge/.env |
| heartbeat_timeout_s | 5.0 | configs/edge_node.yml |
| profile_collect --interval | 1.0 | mặc định |
| offload thresholds L3/L2/L1 | 57/65/75 | configs/edge_node.yml |
| reclaim_margin | 7 | configs/edge_node.yml |
| reclaim_stable_s | 15 | configs/edge_node.yml |

Các cấu hình trên xác định ranh giới runtime hiện tại. `LOAD_POLICY=actual` và
`LOAD_MODEL=formula` khẳng định hệ thống đang chạy reactive baseline. `proactive.enabled=false`
khẳng định predictor chưa tham gia policy. `HEALTH_INTERVAL=1.0` và
`profile_collect --interval=1.0` khẳng định mọi reader phải đi theo cadence 1 giây của
snapshot writer. Các threshold L3/L2/L1 gắn với FPS anchor, không phải GPU anchor.

3.3.3. Offline rescue (peer-offline failover)

Khi peer không heartbeat trong 5s, survivors đồng thời detect. Consistent hash
(SHA-256) → deterministic chọn rescuer cho orphan camera. Jitter 0–3s tránh
thundering herd. Rescuer verify RTSP reachability → ADD. Camera được cứu sẽ
được track và trả về owner khi online. Đây là tính năng availability bắt buộc,
không phải optional optimization.

Offline rescue cần được phân tích như một path độc lập với overload offload. Trong
overload, node nguồn còn sống và chủ động yêu cầu giảm tải. Trong offline rescue, node
nguồn đã mất heartbeat; các survivor phải suy luận camera orphan từ state đã biết và
tự quyết định ai cứu. Vì không có leader, consistent hashing làm cơ chế phân công tối
thiểu đủ dùng. Nếu owner quay lại, rebalance path sẽ trả camera về owner khi điều kiện
ổn định.

3.3.4. Camera reclaim

Khi load giảm (load < 50) trong 15s, node gửi ADD để reclaim camera đã migrate.
Make-Before-Break: ADD về self → đợi PLAYING → REMOVE ở holder.

Camera reclaim thể hiện tính tự phục hồi của hệ thống. Nếu chỉ có offload mà không có
reclaim, hệ thống sẽ tích lũy trạng thái lệch owner sau mỗi đợt quá tải. Nếu reclaim
quá nhanh, hệ thống lại tạo ping-pong. Vì vậy reclaim cần low-load evidence trong
`reclaim_stable_s` và vẫn dùng Make-Before-Break. Điều này giữ cùng nguyên tắc an toàn
cho cả chiều chuyển đi và chiều lấy lại.

3.4. Phương pháp thực nghiệm

3.4.1. Mục tiêu đánh giá

Phần thực nghiệm cần kiểm tra hai nhóm câu hỏi. Nhóm thứ nhất là hệ thống đã giữ đúng
contract triển khai hay chưa: telemetry có mới, health có đến Server, collector có loại
duplicate/stale, timeout có thống nhất và state machine có tuân thủ guard. Nhóm thứ hai
là Multi-Edge Coordination có tạo lợi ích vận hành so với baseline không coordination
hay không: khi một node quá tải hoặc mất kết nối, FPS, thời gian phục hồi và trạng thái
camera có tốt hơn so với việc để từng node chạy đơn lẻ hay không.

Với phạm vi hiện tại, báo cáo đã có bằng chứng regression ở mức code/host và protocol
thực nghiệm phần cứng cần chạy tiếp trên Jetson. Do đó bảng kết quả được chia rõ thành
"đã xác nhận" và "cần đo trên hardware". Cách chia này giữ claim khớp với evidence:
test suite bảo vệ logic và contract, còn kết luận về QoS end-to-end phải dựa trên
receiving-side runtime measurement.

3.4.2. Môi trường và điều kiện đo

Môi trường triển khai mục tiêu gồm tối thiểu hai Jetson trong cùng LAN, camera RTSP
hoặc MediaMTX stream, Server aiohttp, dashboard browser và cấu hình node/camera ổn
định. Mỗi Jetson chạy `Edge/run_edge.sh` để bảo đảm HealthAgent, pipeline và collector
nằm trong cùng workflow. Khi thu thập dữ liệu, lệnh cần dùng `--collect-interval 1.0`
để khớp cadence 1 giây của SpeedProbe writer và HealthAgent reader.

Mỗi lần đo cần ghi lại `NODE_ID`, danh sách camera active, thời điểm bắt đầu/kết thúc,
`session_id`, dải `sequence`, FPS trung bình, input FPS, load score, quyết định
offload/migration, Server health receiving và trạng thái dashboard. Các kết quả liên
quan migration/failover chỉ được tính thành công khi có bằng chứng ở phía nhận: target
ACK `PLAYING`, stream xuất hiện ở receiver, Server/browser nhận state cuối hoặc survivor
ADD được camera orphan.

3.4.3. Baseline và metric

Baseline chính là Single Edge hoặc Multi-Edge không migration: mỗi node xử lý camera
của chính nó và không chuyển camera khi load score tăng. Cấu hình đề xuất được so sánh
với baseline này là Multi-Edge reactive coordination: node vẫn tự trị, nhưng có health
P2P, load score, multi-level offload, Make-Before-Break migration, reclaim và
peer-offline rescue.

Các metric cần đo trực tiếp gồm FPS trung bình và FPS tối thiểu theo camera, tỷ lệ thời
gian FPS dưới các ngưỡng L3/L2/L1, thời gian từ overload đến decision, thời gian từ ADD
đến target `PLAYING`, số frame hoặc thời gian gián đoạn quan sát ở receiving side, thời
gian peer-offline rescue, số duplicate/stale row trong CSV và thời gian dashboard nhận
health/offline update. Không cần quá nhiều metric phụ; các metric trên đủ gắn với lợi
ích cốt lõi của kiến trúc: duy trì QoS, giảm gián đoạn và phục hồi khi peer lỗi.

3.4.4. Kịch bản kiểm thử

| STT | Kịch bản | Điều kiện thành công | Metric chính |
|---|---|---|---|
| 1 | Runtime bình thường | Server/browser nhận health mỗi 1s, FPS ổn định | FPS, health latency |
| 2 | Baseline không coordination | Node quá tải nhưng không migration | time under L3/L2/L1 |
| 3 | Multi-Edge overload | Load vượt dwell, target nhận việc, source tiếp tục chạy | FPS sau can thiệp, decision latency |
| 4 | Full stream migration | Target ACK `PLAYING` trước source `REMOVE` | migration time, receiver gap |
| 5 | Reclaim | Load < 50 trong 15s, camera về owner bằng Make-Before-Break | reclaim time, FPS sau reclaim |
| 6 | Peer failure | Survivor detect timeout 5s, ADD orphan camera | rescue time, camera availability |
| 7 | Collection | CSV có `session_id`, `sequence` tăng, không stale/duplicate | rejected rows, valid rows |
| 8 | Dashboard | Browser nhận health/offline/update cuối | receiving-side state |

Bảng 3.2. Kịch bản thực nghiệm và điều kiện thành công

3.5. Kết quả và phân tích

3.5.1. Kết quả regression hiện tại

Repository hiện có unit tests và focused regression tests. Lệnh kiểm tra là
`conda run -n DoAn python3 -m pytest`; kết quả hiện tại là 296 tests pass. Kết quả này
xác nhận các contract đã được mã hóa trong test suite, bao gồm một số parser, timeout,
policy guard, state transition và path tính toán. Đây là bằng chứng cần thiết để hệ
thống có nền hồi quy trước khi đem lên Jetson.

Giá trị của kết quả 296 tests pass nằm ở việc ngăn các contract đã chuẩn hóa bị phá vỡ
khi sửa code. Nó chưa thay thế được thử nghiệm phần cứng vì DeepStream, TensorRT,
NVDEC, thermal throttling, RTSP network và MediaMTX behavior không được host-only tests
mô phỏng đầy đủ. Vì vậy kết luận đúng là: codebase có regression baseline ổn định;
kết luận về QoS end-to-end cần phép đo Jetson và receiving-side evidence.

Hình 3.3. Kết quả tập hợp test suite — 296 tests pass.

3.5.2. Kết quả thiết kế đã xác nhận bằng code/config

Đối chiếu với runtime hiện tại cho thấy hệ thống đã đạt các điểm triển khai chính.
Health cadence là 1 giây; heartbeat timeout thống nhất 5 giây ở Edge và Server;
snapshot telemetry được ghi atomic, có `session_id` và `sequence`; collector tự reject
duplicate/stale row; load score là FPS-dominant composite có workload và thermal bonus;
GPU utilization chỉ dùng để quan sát, không làm trigger độc lập; PeerOrchestrator chạy
state machine phân tán với offload, migration, reclaim và peer-offline rescue.

Các kết quả này chứng minh phần reactive Multi-Edge runtime đã có cơ sở kỹ thuật rõ
ràng. Hệ thống không phụ thuộc Server làm central controller; Server giữ vai trò
observer qua registry, WebSocket, REST API và dashboard. Quyết định điều phối nằm trên
từng Edge node, còn Zenoh cung cấp kênh P2P để chia sẻ health, camera context, control
message và ACK.

3.5.3. Phân tích lợi ích kỳ vọng so với baseline

So với baseline không coordination, Multi-Edge reactive coordination có ba lợi ích cần
được đo. Thứ nhất, khi một node quá tải, full stream migration L1 có thể giảm số camera
đang xử lý trên node nguồn, từ đó kỳ vọng giảm thời gian FPS nằm dưới ngưỡng L3/L2/L1.
Thứ hai, Make-Before-Break giảm rủi ro mất quan sát vì target phải ACK `PLAYING` trước
khi source REMOVE. Thứ ba, peer-offline rescue giúp camera của node chết không biến mất
khỏi cụm nếu survivor còn capacity và còn camera_config hợp lệ.

Ở thời điểm hiện tại, lợi ích trên là claim cần đo bằng protocol tại mục 3.4, không
được thay bằng số lượng test pass. Báo cáo vì vậy chỉ khẳng định phần đã chắc: runtime
có cơ chế để tạo lợi ích đó và đã có contract để đo lợi ích. Bước còn thiếu là chạy
baseline và Multi-Edge trên cùng điều kiện Jetson, sau đó so sánh FPS, migration time,
receiver gap, rescue time và dashboard receiving state.

3.5.4. Những kết luận không được suy diễn quá mức

Proactive/DL predictor chưa được bật làm policy triển khai, nên không thể kết luận mô
hình dự báo đã cải thiện QoS trên Jetson. L2/L3 crop offload không được xem là đã giảm
FPS source giống L1, vì source vẫn xử lý frame gốc; chỉ L1 full stream migration mới
thật sự giảm số camera trong pipeline nguồn. GPU utilization cũng không được dùng làm
load trigger chính vì trên Jetson nó dễ bị alias bởi TensorRT burst, DVFS và không bao
quát NVDEC/VIC.

Make-Before-Break là cơ chế giảm rủi ro gián đoạn, không phải bằng chứng tuyệt đối về
không mất frame. Số frame mất hoặc thời gian gián đoạn phải đo ở receiving side, nơi
người vận hành thật sự xem stream hoặc dashboard. Tương tự, producer log báo đã gửi
health hoặc ADD message chưa đủ; Server, browser hoặc target Edge phải xác nhận đã nhận
và chuyển trạng thái đúng.

3.6. Hạn chế và hướng phát triển

3.6.1. Hạn chế

Hạn chế thứ nhất là bằng chứng hiện tại chủ yếu ở mức code/host regression. Các hành vi
Jetson-only như TensorRT engine loading, NVDEC decode, GPU memory pressure, thermal,
MediaMTX, RTSP jitter và browser playback vẫn cần đo trên phần cứng thật.

Hạn chế thứ hai là chưa có baseline thực nghiệm hoàn chỉnh giữa Single Edge hoặc
Multi-Edge không migration và Multi-Edge reactive coordination. Vì vậy báo cáo chưa
đưa ra số liệu định lượng cuối cùng cho mức cải thiện FPS, thời gian phục hồi hoặc tỷ
lệ duy trì QoS.

Hạn chế thứ ba là proactive/DL predictor chưa được triển khai trong runtime Jetson.
Nhánh này chỉ nên được xem là candidate cho hướng phát triển sau khi có dữ liệu thật,
baseline persistence/slope/margin, evaluator không leakage và lead time lớn hơn latency
offload/migration.

Hạn chế thứ tư là L2/L3 crop offload cần engine và verification riêng trước khi coi là
kết quả triển khai đầy đủ. Path violation/dashboard cũng cần receiving-side validation
nếu muốn kết luận violation feed hoàn chỉnh end-to-end.

3.6.2. Hướng phát triển

Hướng phát triển trước mắt là hoàn thiện bộ thực nghiệm Jetson theo protocol tại mục
3.4. Cụ thể, cần chạy baseline không coordination và Multi-Edge coordination trên cùng
cấu hình camera, ghi FPS, load score, migration time, receiver gap, rescue time và
dashboard receiving state. Đây là bước trực tiếp nhất để chuyển báo cáo từ "hệ thống đã
xây dựng" sang "hệ thống đã được đánh giá định lượng".

Hướng tiếp theo là hoàn thiện L2/L3 crop offload engine, mở rộng integration tests cho
HealthAgent publish loop, Server watchdog, snapshot lifecycle và ONNX runtime path. Với
proactive/DL, chỉ nên triển khai sau khi mô hình dự báo đánh bại heuristic baselines
trên dữ liệu thật và lead time đủ lớn để quyết định có ích cho migration/offload.

Cuối cùng, nếu số cụm Edge tăng vượt phạm vi LAN, hệ thống có thể xem xét Zenoh router
mode hoặc multi-cluster routing. Đây là hướng mở rộng hạ tầng, không phải yêu cầu của
runtime reactive hiện tại.

-------------------------------------------------------------------------------------
KẾT LUẬN VÀ HƯỚNG PHÁT TRIỂN

1. Kết luận chung

Đề tài đã xây dựng được một reactive Multi-Edge Coordination runtime cho xử lý nhiều
camera giao thông trên Edge node. Hệ thống giải quyết phần lõi của bài toán: mỗi node
tự đo QoS, xuất bản health, nhận trạng thái peer, ra quyết định offload/migration theo
load score, đồng thời giữ Server ở vai trò quan sát thay vì central controller.

Kết quả chắc nhất của đề tài là bộ contract runtime đã được chuẩn hóa: health cadence
1 giây, heartbeat timeout 5 giây trên Edge và Server, unified snapshot atomic có
`session_id`/`sequence`, collector loại duplicate/stale, FPS-first load score,
PeerOrchestrator có offload/reclaim/failover và Make-Before-Break migration. Test suite
hiện tại đạt 296 unit/focused regression tests passing, tạo nền kiểm soát hồi quy cho
các contract này.

2. Đóng góp chính

- Kiến trúc P2P phân tán cho xử lý đa camera giao thông trên Jetson.
- Telemetry snapshot contract: atomic write + validation ở mỗi consumer,
  ngăn duplicate/stale propagation.
- Load score composite ưu tiên FPS: tránh trigger sai từ GPU bursts.
- Cơ chế failover/rescue bắt buộc bằng consistent hashing, không cần leader.
- Heartbeat timeout chuẩn hóa 5s làm single source of truth cho Edge + Server.
- Protocol đánh giá tách rõ code regression, baseline comparison và receiving-side
  validation để tránh claim vượt quá bằng chứng.

3. Hạn chế

- Hệ thống là reactive baseline; proactive DL chưa triển khai trên Jetson.
- L2/L3 crop offload chưa có đủ engine để kiểm chứng full offload inference.
- Thời gian failover (5s + jitter + RTSP) chưa được đo trên hardware thực.
- Host-only tests không thay thế real camera + Jetson.
- Chưa có bảng số liệu baseline hoàn chỉnh giữa không coordination và Multi-Edge
  coordination trên cùng điều kiện Jetson.

4. Hướng phát triển

1. End-to-end verification trên Jetson với receiving-side proof.
2. Đánh giá model dự báo trên real Mode-A data, so sánh vs heuristic baselines.
3. Deploy L2/L3 crop offload engines.
4. Mở rộng multi-cluster qua Zenoh router.
5. Tăng coverage integration tests.

-------------------------------------------------------------------------------------
TÀI LIỆU THAM KHẢO

[1] NVIDIA Corporation. NVIDIA DeepStream SDK Documentation. Truy cập ngày 6
    tháng 9 năm 2026, từ https://docs.nvidia.com/metropolis/deepstream/
[2] NVIDIA Corporation. NVIDIA Jetson AGX Orin Series Datasheet. Truy cập ngày
    6 tháng 9 năm 2026, từ https://www.nvidia.com/en-us/autonomous-machines/
    embedded-systems/jetson-orin/
[3] Eclipse Foundation. Eclipse Zenoh Documentation. Truy cập ngày 6 tháng 9
    năm 2026, từ https://zenoh.io/docs/
[4] Szeliski, R. Computer Vision: Algorithms and Applications. Springer Science
    & Business Media.
[5] Bradski, G. và Kaehler, A. Learning OpenCV: Computer Vision with the
    OpenCV Library. O'Reilly Media, Inc.
[6] OpenCV Team. OpenCV Library Documentation. Truy cập ngày 6 tháng 9 năm
    2026, từ https://docs.opencv.org/
[7] Ultralytics. YOLOv11 Documentation. Truy cập ngày 6 tháng 9 năm 2026,
    từ https://docs.ultralytics.com/
[8] The GStreamer Project. GStreamer Documentation. Truy cập ngày 6 tháng 9
    năm 2026, từ https://gstreamer.freedesktop.org/
[9] Paszke, A. et al. PyTorch: An Imperative Style, High-Performance Deep
    Learning Library. Advances in Neural Information Processing Systems
    (NeurIPS).
[10] Boulkenafet, Z. et al. License Plate Recognition and Detection: A
    Comprehensive Survey. IEEE Transactions on Intelligent Transportation
    Systems.
[11] Echeverria, S. et al. A Zero-Overhead Load Balancer for Edge Computing.
    IEEE International Conference on Edge Computing (EDGE).
[12] Msgpack.org. MessagePack: Efficient Serialization. Truy cập ngày 6 tháng 9
    năm 2026, từ https://msgpack.org/
[13] MediaMTX Project. MediaMTX: Self-hosted WebRTC media server. Truy cập
    ngày 6 tháng 9 năm 2026, từ https://github.com/bluenviron/mediamtx
[14] aiohttp contributors. aiohttp Documentation. Truy cập ngày 6 tháng 9 năm
    2026, từ https://docs.aiohttp.org/
[15] NVIDIA Corporation. TensorRT Developer Documentation.
[16] Redmon, J. et al. You Only Look Once: Unified, Real-Time Object Detection.
    Proceedings of the IEEE Conference on Computer Vision and Pattern
    Recognition (CVPR).
