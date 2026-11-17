# Real-Time License Plate Recognition & Smart Parking System

An end-to-end computer vision and web-based parking management system that detects vehicles, reads license plates in real time, verifies detections across multiple frames, and manages vehicle entry/exit logs, automated parking fees, and guard shift audits.

## Key Features
- **Dual-Stage Deep Learning Detection (`app.py`):** Uses **YOLOv8** (`yolov8s.pt`) with ByteTrack object tracking to detect and classify vehicles (`Car` and `MC`), paired with a custom-trained YOLOv8 license plate detector (`best.pt`) and **EasyOCR** for alphanumeric plate extraction.
- **Smooth MJPEG Live Streaming:** Runs AI inference every 3rd frame while caching bounding-box overlays on intermediate frames for fluid 720p web streaming with freeze-on-pause controls.
- **Multi-Frame Verification & Deduplication (`parking_system.py`):** Requires 3 consistent frame detections (`VERIFY_REQUIRED_COUNT = 3`) with a 20-second cooldown before alerting the on-duty guard, minimizing false positives.
- **Automated Parking Fee Calculation:** Computes duration-based parking fees automatically upon exit (`₱10/hr` up to `₱80` max for Cars; `₱5/hr` up to `₱40` max for Motorcycles) with manual time-out overrides for guards.
- **Role-Based Web Dashboard (`smart_parking_web.py`):**
  - **Guard Portal:** Live camera feed, real-time incoming/exiting plate confirmation and correction modal, active vehicle tracking, manual time-out controls, and incident reporting.
  - **Admin / Supervisor Portal:** Live occupancy and daily revenue metrics, guard shift cash reconciliation (flagging variances between expected system revenue and declared cash), date-filtered transaction logs, and guard account management.
- **Database & CSV Logging:** Persists all parking events, guard shifts, and incident reports in **SQLite** (`results/smart_parking.db`) alongside CSV exports (`results/LPR_results.csv`).

## Tech Stack
- **Computer Vision & AI:** Python, OpenCV, Ultralytics YOLOv8, ByteTrack, EasyOCR, PyTorch (CUDA / CPU)
- **Backend & Database:** Python `ThreadingHTTPServer`, SQLite3, SHA-256 Salted Authentication
- **Frontend:** HTML5, CSS3, Vanilla JavaScript (Live MJPEG & AJAX polling)

## Project Structure
```text
├── smart_parking_web.py   # Main web server, role-based dashboards, and REST/MJPEG endpoints
├── app.py                 # YOLOv8 + EasyOCR detection pipeline and background video thread
├── parking_system.py      # SQLite schema, multi-frame verification, fee & shift audit logic
├── camera.py              # OpenCV VideoCapture wrapper with auto-reconnect support
├── check_cuda.py          # GPU / CUDA availability diagnostic script
├── util.py                # CSV logging and plate character formatting helpers
├── requirements.txt       # Python dependencies
├── LICENSE                # MIT License
└── models/                # Pre-trained YOLOv8 weights (best.pt, yolov8s.pt)
```

## Setup & Installation
1. **Clone the repository:**
   ```bash
   git clone [https://github.com/mrnatrenchi/plate-number-recognition-monitoring-system.git](https://github.com/mrnatrenchi/plate-number-recognition-monitoring-system.git)
   cd plate-number-recognition-monitoring-system
   ```
2. **Create and activate a virtual environment:**
   ```bash
   python -m venv venv
   venv\Scripts\activate      # Windows
   # source venv/bin/activate  # macOS/Linux
   ```
3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   pip install torch torchvision --index-url [https://download.pytorch.org/whl/cu121](https://download.pytorch.org/whl/cu121)
   ```
   *(Verify GPU support with `python check_cuda.py`; the system automatically falls back to CPU if CUDA is unavailable.)*
4. **Ensure model weights are inside `./models/`:**
   - `./models/best.pt` (custom-trained license plate detector)
   - `./models/yolov8s.pt` (COCO vehicle detector)

## Usage
Start the web server and background detection pipeline:
```bash
python smart_parking_web.py
```
Open **`http://127.0.0.1:5000`** in your browser, register or log in as a **Guard** or **Admin**, and start a shift to begin live monitoring. *(By default, `app.py` connects to an available webcam and falls back to `test.mp4` if no camera is detected. To run the OpenCV preview window without the web UI, run `python app.py`.)*

## Contributors
- **Marian Mindoro** — System Integration, Web Dashboard & Testing
- **Karl Santiago Baydal Bernaldez** — Project Leader & Backend Developer
- **Juan Rodrigo Badiola** — Data Analyst & Tester
- **Ken Evan Soltes** — Tester & Documentation Specialist

## License
This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.
