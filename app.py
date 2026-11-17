import cv2
import numpy as np
from ultralytics import YOLO
import easyocr
from util import write_csv
import os, time, threading
import torch
from parking_system import verify_and_record_detection
from camera import CameraSource

# ── Folders ───────────────────────────────────────────────────────────────────
lp_folder_path   = "./licenses_plates_imgs_detected/"
vehicle_folder   = "./vehicles/"
results_folder   = "./results/"
for folder in [lp_folder_path, vehicle_folder, results_folder]:
    os.makedirs(folder, exist_ok=True)

# ── Models ────────────────────────────────────────────────────────────────────
LICENSE_MODEL_DETECTION_DIR = './models/best.pt'
COCO_MODEL_DIR              = "./models/yolov8s.pt"

use_gpu = torch.cuda.is_available()
device  = 'cuda' if use_gpu else 'cpu'
print(f"--- System running on: {device.upper()} ---")

reader                  = easyocr.Reader(['en'], gpu=use_gpu)
coco_model              = YOLO(COCO_MODEL_DIR).to(device)
license_plate_detector  = YOLO(LICENSE_MODEL_DETECTION_DIR).to(device)

vehicles  = {2: "Car", 3: "MC",}

# ── Colors (BGR) ──────────────────────────────────────────────────────────────
COLOR_VEHICLE   = (255, 150,   0)   # Blue  — vehicle bounding box
COLOR_PLATE     = (  0, 255,  80)   # Green — license plate bounding box
COLOR_LABEL_VEH = (  0, 255, 255)   # Yellow — vehicle label text
COLOR_LABEL_LP  = (255, 255, 255)   # White  — plate text
COLOR_ACTION    = (  0, 255, 255)   # Cyan   — ENTRY/EXIT PENDING label

# ── Shared frame buffer for MJPEG streaming ───────────────────────────────────
_latest_frame = None
_frame_lock   = threading.Lock()

def get_latest_frame():
    """Return the latest JPEG-encoded annotated frame as bytes, or None."""
    with _frame_lock:
        return _latest_frame

# ── OCR ───────────────────────────────────────────────────────────────────────
def read_license_plate(license_plate_crop):
    scores = 0
    detections = reader.readtext(
        license_plate_crop,
        allowlist='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    )
    if not detections:
        return None, None

    rectangle_size = license_plate_crop.shape[0] * license_plate_crop.shape[1]
    plate = []

    for result in detections:
        bbox, text, score = result
        length = np.sum(np.subtract(result[0][1], result[0][0]))
        height = np.sum(np.subtract(result[0][2], result[0][1]))
        if (length * height) / rectangle_size > 0.17:
            text = text.upper().replace(" ", "")
            scores += score
            plate.append(text)

    if plate:
        return "".join(plate), scores / len(plate)
    return None, 0

# ── Per-frame detection ───────────────────────────────────────────────────────
def model_prediction(img):
    license_numbers = 0
    results         = {}
    vehicle_bboxes  = []

    object_detections = coco_model.track(img, persist=True, tracker="bytetrack.yaml")[0]

    if object_detections.boxes.data is not None:
        for detection in object_detections.boxes.data.tolist():
            if len(detection) == 7:
                x1, y1, x2, y2, track_id, score, class_id = detection
                if int(class_id) in vehicles:
                    class_name = vehicles[int(class_id)]
                    vehicle_bboxes.append([track_id, x1, y1, x2, y2, score, class_name])

                    # ── Blue box around vehicle ──────────────────────────────
                    cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), COLOR_VEHICLE, 2)
                    cv2.putText(img, f"{class_name}-{int(track_id)}",
                                (int(x1), int(y1) - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLOR_LABEL_VEH, 2)

    if vehicle_bboxes:
        lp_detections = license_plate_detector.track(img, persist=True)[0]
        if lp_detections.boxes.data is not None:
            for lp in lp_detections.boxes.data.tolist():
                if len(lp) == 7:
                    lx1, ly1, lx2, ly2, lp_track_id, lp_score, _ = lp
                    for veh in vehicle_bboxes:
                        v_id, vx1, vy1, vx2, vy2, v_score, v_name = veh
                        if lx1 > vx1 and lx2 < vx2 and ly1 > vy1 and ly2 < vy2:
                            lp_crop = img[int(ly1):int(ly2), int(lx1):int(lx2)]
                            if lp_crop.size == 0:
                                continue

                            lp_gray = cv2.cvtColor(lp_crop, cv2.COLOR_BGR2GRAY)
                            lp_gray = cv2.resize(lp_gray, (0, 0), fx=3, fy=3)

                            lp_text, lp_text_score = read_license_plate(lp_gray)

                            if lp_text and lp_text_score >= 0.5:
                                parking_event = verify_and_record_detection(lp_text, v_name)
                                lp_filename = f'{lp_text}_{int(lp_track_id)}.jpg'
                                cv2.imwrite(os.path.join(lp_folder_path, lp_filename), lp_gray)

                                # ── Green box around license plate ───────────
                                cv2.rectangle(img, (int(lx1), int(ly1)), (int(lx2), int(ly2)), COLOR_PLATE, 2)
                                cv2.putText(img, lp_text, (int(lx1), int(ly1) - 10),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, COLOR_LABEL_LP, 2)
                                if parking_event.get("verified"):
                                    cv2.putText(img,
                                                f"{parking_event.get('action','pending').upper()} PENDING",
                                                (int(lx1), int(ly2) + 25),
                                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, COLOR_ACTION, 2)

                                results[license_numbers] = {
                                    'Vehicle': {'id': v_id, 'class': v_name},
                                    'License': {'text': lp_text, 'score': lp_text_score}
                                }
                                license_numbers += 1
                                write_csv(results, "./results/LPR_results.csv")
    return img

# ── Background thread: reads webcam, runs detection ──────────────────────
USE_WEBCAM         = True   # Use webcam instead of video file
VIDEO_PATH         = "test.mp4"  # Fallback video file if webcam fails
PROCESS_EVERY_N    = 3      # run AI on every 3rd frame for better accuracy
LOOP_VIDEO         = False  # Not applicable for webcam

# ── Box-caching helpers ───────────────────────────────────────────────────────

def _draw_cached_boxes(img, boxes):
    """Draw the last-known bounding boxes on a raw frame without running AI."""
    out = img.copy()
    for (x1, y1, x2, y2, label, color) in boxes:
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        cv2.putText(out, label, (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    return out

def _model_prediction_with_boxes(img):
    """
    Same as model_prediction() but also returns a flat list of drawn boxes
    so they can be re-applied to subsequent raw frames cheaply.
    Returns (annotated_img, boxes_list).
    """
    boxes           = []   # (x1,y1,x2,y2,label,color)
    vehicle_bboxes  = []
    results         = {}
    license_numbers = 0

    object_detections = coco_model.track(img, persist=True, tracker="bytetrack.yaml")[0]

    if object_detections.boxes.data is not None:
        for detection in object_detections.boxes.data.tolist():
            if len(detection) == 7:
                x1, y1, x2, y2, track_id, score, class_id = detection
                if int(class_id) in vehicles:
                    class_name = vehicles[int(class_id)]
                    vehicle_bboxes.append([track_id, x1, y1, x2, y2, score, class_name])
                    label = f"{class_name}-{int(track_id)}"
                    cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), COLOR_VEHICLE, 2)
                    cv2.putText(img, label, (int(x1), int(y1) - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, COLOR_LABEL_VEH, 2)
                    boxes.append((int(x1), int(y1), int(x2), int(y2), label, COLOR_VEHICLE))

    if vehicle_bboxes:
        lp_detections = license_plate_detector.track(img, persist=True)[0]
        if lp_detections.boxes.data is not None:
            for lp in lp_detections.boxes.data.tolist():
                if len(lp) == 7:
                    lx1, ly1, lx2, ly2, lp_track_id, lp_score, _ = lp
                    for veh in vehicle_bboxes:
                        v_id, vx1, vy1, vx2, vy2, v_score, v_name = veh
                        if lx1 > vx1 and lx2 < vx2 and ly1 > vy1 and ly2 < vy2:
                            lp_crop = img[int(ly1):int(ly2), int(lx1):int(lx2)]
                            if lp_crop.size == 0:
                                continue
                            lp_gray = cv2.cvtColor(lp_crop, cv2.COLOR_BGR2GRAY)
                            lp_gray = cv2.resize(lp_gray, (0, 0), fx=3, fy=3)
                            lp_text, lp_text_score = read_license_plate(lp_gray)
                            if lp_text and lp_text_score >= 0.5:
                                parking_event = verify_and_record_detection(lp_text, v_name)
                                lp_filename = f'{lp_text}_{int(lp_track_id)}.jpg'
                                cv2.imwrite(os.path.join(lp_folder_path, lp_filename), lp_gray)
                                cv2.rectangle(img, (int(lx1), int(ly1)), (int(lx2), int(ly2)), COLOR_PLATE, 2)
                                cv2.putText(img, lp_text, (int(lx1), int(ly1) - 10),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, COLOR_LABEL_LP, 2)
                                boxes.append((int(lx1), int(ly1), int(lx2), int(ly2), lp_text, COLOR_PLATE))
                                if parking_event.get("verified"):
                                    action_label = f"{parking_event.get('action','pending').upper()} PENDING"
                                    cv2.putText(img, action_label,
                                                (int(lx1), int(ly2) + 25),
                                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, COLOR_ACTION, 2)
                                results[license_numbers] = {
                                    'Vehicle': {'id': v_id, 'class': v_name},
                                    'License': {'text': lp_text, 'score': lp_text_score}
                                }
                                license_numbers += 1
                                write_csv(results, "./results/LPR_results.csv")

    return img, boxes

_stop_event       = threading.Event()
_detection_paused = threading.Event()   # set = paused, clearing = running

def pause_detection():
    _detection_paused.set()

def resume_detection():
    _detection_paused.clear()

def is_detection_paused():
    return _detection_paused.is_set()

def detection_loop():
    """
    Reads frames from webcam or video file, runs YOLO + OCR,
    stores the annotated frame for MJPEG streaming,
    and pushes detections into the DB via verify_and_record_detection().
    
    For webcam: continuous live feed
    For video: loops indefinitely so the feed never goes dark

    Strategy for smooth playback:
    - Every single frame is pushed to the MJPEG buffer (no sleep, no skipping).
    - YOLO/OCR only runs every PROCESS_EVERY_N frames.
    - Between AI frames, the raw frame is pushed so motion stays fluid.
    - The last bounding-box overlay is blended onto raw frames so boxes
      don't vanish between AI runs.
    """
    global _latest_frame
    frame_count    = 0
    last_boxes     = []   # list of (x1,y1,x2,y2,label,color) from last AI run
    
    if USE_WEBCAM:
        print("[Detection] Starting webcam feed...")
        # Try to find available webcam
        devices = CameraSource.list_devices()
        if not devices:
            print("[Detection] ERROR: No webcams found. Falling back to video file.")
            cap = cv2.VideoCapture(VIDEO_PATH)
        else:
            print(f"[Detection] Using webcam index {devices[0]}")
            cam = CameraSource(device=devices[0], width=1280, height=720, backend=cv2.CAP_DSHOW)
            if not cam.open():
                print("[Detection] ERROR: Cannot open webcam. Falling back to video file.")
                cap = cv2.VideoCapture(VIDEO_PATH)
                cam = None
            else:
                cap = None
    else:
        print(f"[Detection] Starting video feed from: {VIDEO_PATH}")
        cap = cv2.VideoCapture(VIDEO_PATH)
        cam = None

    # Determine FPS
    if cam:
        video_fps = cam.fps
    elif cap and cap.isOpened():
        video_fps = cap.get(cv2.CAP_PROP_FPS) or 25
    else:
        video_fps = 30
    
    frame_delay = 1.0 / video_fps

    while not _stop_event.is_set():
        # ── Pause gate — hold here while paused, don't consume frames ──
        if _detection_paused.is_set():
            time.sleep(0.1)
            continue

        t0 = time.time()
        
        # Read frame from webcam or video
        if cam:
            ret, frame = cam.read()
            if not ret:
                print("[Detection] Webcam read failed. Attempting to reconnect...")
                cam.release()
                time.sleep(1)
                if not cam.open():
                    print("[Detection] Webcam reconnection failed. Stopping.")
                    break
                continue
        elif cap:
            ret, frame = cap.read()
            if not ret:
                if LOOP_VIDEO:
                    print("[Detection] Video ended. Looping...")
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    frame_count = 0
                    last_boxes = []
                    continue
                else:
                    print("[Detection] Video ended. Detection stopped.")
                    break
        else:
            print("[Detection] ERROR: No video source available.")
            time.sleep(5)
            continue

        frame_count += 1
        frame_resized = cv2.resize(frame, (1280, 720))

        if frame_count % PROCESS_EVERY_N == 0:
            # Run AI on this frame — also updates last_boxes
            try:
                annotated, last_boxes = _model_prediction_with_boxes(frame_resized)
            except Exception as e:
                print(f"[Detection] Frame error: {e}")
                annotated = frame_resized
        else:
            # Draw the cached boxes on the raw frame (no AI cost)
            annotated = _draw_cached_boxes(frame_resized, last_boxes)

        _, buf = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
        with _frame_lock:
            _latest_frame = buf.tobytes()

        # Sleep only the time remaining to match fps
        elapsed = time.time() - t0
        remaining = frame_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    # Cleanup
    if cam:
        cam.release()
    if cap:
        cap.release()
    print("[Detection] Detection loop stopped.")

def start_detection_thread():
    t = threading.Thread(target=detection_loop, daemon=True)
    t.start()
    return t

def stop_detection():
    _stop_event.set()

# ── Standalone run (optional, shows a preview window) ────────────────────────
def main():
    if USE_WEBCAM:
        print("[Standalone] Using webcam feed...")
        devices = CameraSource.list_devices()
        if not devices:
            print("Error: No webcams found. Falling back to video file.")
            cap = cv2.VideoCapture(VIDEO_PATH)
            cam = None
        else:
            print(f"[Standalone] Using webcam index {devices[0]}")
            cam = CameraSource(device=devices[0], width=1280, height=720, backend=cv2.CAP_DSHOW)
            if not cam.open():
                print("Error: Cannot open webcam. Falling back to video file.")
                cap = cv2.VideoCapture(VIDEO_PATH)
                cam = None
            else:
                cap = None
    else:
        print(f"[Standalone] Reading from {VIDEO_PATH}")
        cap = cv2.VideoCapture(VIDEO_PATH)
        cam = None

    if not cam and (not cap or not cap.isOpened()):
        print("Error: Could not open video source.")
        return

    frame_count   = 0
    last_frame    = None

    while True:
        if cam:
            ret, frame = cam.read()
            if not ret:
                print("Webcam read failed. Attempting to reconnect...")
                cam.release()
                time.sleep(1)
                if not cam.open():
                    print("Reconnection failed. Exiting.")
                    break
                continue
        elif cap:
            ret, frame = cap.read()
            if not ret:
                if LOOP_VIDEO:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    frame_count = 0
                    continue
                else:
                    break
        else:
            break

        frame = cv2.resize(frame, (1280, 720))
        frame_count += 1

        if frame_count > 15 and frame_count % PROCESS_EVERY_N == 0:
            last_frame = model_prediction(frame)

        cv2.imshow("Smart Parking — Live Feed", last_frame if last_frame is not None else frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    if cam:
        cam.release()
    if cap:
        cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()