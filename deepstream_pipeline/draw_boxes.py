import json
import os
from pathlib import Path
import cv2
import numpy as np

TARGET_FOLDER_SUFFIX = "manual/fl1/1788452099"
IMAGES_INPUT_FOLDER = "images/" + TARGET_FOLDER_SUFFIX
PREDICTIONS_INPUT_FOLDER = "predictions/" + TARGET_FOLDER_SUFFIX
OUTPUT_FOLDER = "predictions_drawn/" + TARGET_FOLDER_SUFFIX

# Distinct colours (BGR) cycled per class
CLASS_COLORS = [
    (0, 255, 0),
    (255, 0, 0),
    (0, 0, 255),
    (0, 255, 255),
    (255, 0, 255),
    (255, 255, 0),
    (128, 0, 255),
    (255, 128, 0),
    (0, 128, 255),
    (128, 255, 0),
]

def get_class_color(class_name, class_color_map):
    if class_name not in class_color_map:
        idx = len(class_color_map) % len(CLASS_COLORS)
        class_color_map[class_name] = CLASS_COLORS[idx]
    return class_color_map[class_name]

def parse_prediction_string(pred_str):
    parts = pred_str.split("|")
    if len(parts) < 8:
        print(f"Warning: Skipping malformed prediction string: {pred_str}")
        return None

    return {
        "class_name": parts[0],
        "x": float(parts[1]),
        "y": float(parts[2]),
        "w": float(parts[3]),
        "h": float(parts[4]),
        "confidence": float(parts[5]),
        "img_w": float(parts[6]),
        "img_h": float(parts[7]),
    }

def compute_iou(box_a, box_b):
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])

    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0

    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    return inter / (area_a + area_b - inter)

def find_overlaps(predictions, iou_threshold=0.3):
    """Return list of overlapping prediction pairs (indices + class names)."""
    boxes = []
    for data in predictions:
        x1 = int(data["x"])
        y1 = int(data["y"])
        x2 = int(data["x"] + data["w"])
        y2 = int(data["y"] + data["h"])
        boxes.append((x1, y1, x2, y2))

    overlaps = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            iou = compute_iou(boxes[i], boxes[j])
            if iou >= iou_threshold:
                overlaps.append((
                    predictions[i]["class_name"],
                    predictions[i]["confidence"],
                    predictions[j]["class_name"],
                    predictions[j]["confidence"],
                    iou,
                ))
    return overlaps

def draw_predictions(image_path, predictions_path, output_path, class_color_map):
    image = cv2.imread(image_path)
    if image is None:
        raise RuntimeError(f"Could not read image from path: {image_path}")

    img_h, img_w = image.shape[:2]

    try:
        with open(predictions_path, "r") as f:
            predictions_list = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Could not parse JSON file '{predictions_path}': {e}")

    parsed = []
    for pred_str in predictions_list:
        data = parse_prediction_string(pred_str)
        if data:
            parsed.append(data)
    save_flag = False
    for data in parsed:
        
        x1 = int(data["x"])
        y1 = int(data["y"])
        x2 = int(data["x"] + data["w"])
        y2 = int(data["y"] + data["h"])

        x1 = max(0, min(x1, img_w - 1))
        y1 = max(0, min(y1, img_h - 1))
        x2 = max(0, min(x2, img_w - 1))
        y2 = max(0, min(y2, img_h - 1))

        box_color = get_class_color(data["class_name"], class_color_map)
        thickness = 4
        cv2.rectangle(image, (x1, y1), (x2, y2), box_color, thickness)

        label = f"{data['class_name']}: {data['confidence']:.2f}"
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 1.0
        text_thickness = 2
        (text_w, text_h), baseline = cv2.getTextSize(label, font, font_scale, text_thickness)

        label_y_top = y1 - text_h - 5
        if label_y_top < 0:
            bg_top = y1
            bg_bottom = y1 + text_h + 5
            text_org = (x1, y1 + text_h + 2)
        else:
            bg_top = y1 - text_h - 5
            bg_bottom = y1
            text_org = (x1, y1 - 5)

        cv2.rectangle(image, (x1, bg_top), (x1 + text_w, bg_bottom), box_color, -1)
        cv2.putText(
            image,
            label,
            text_org,
            font,
            font_scale,
            (0, 0, 0),
            text_thickness,
            cv2.LINE_AA,
        )
        save_flag = True

    cv2.imwrite(output_path, image)
    #if save_flag:
    #    cv2.imwrite(output_path, image)

    print(f"Successfully wrote annotated image: {output_path}")

    # Check for overlaps
    overlaps = find_overlaps(parsed)
    return overlaps

def main():
    image_files = os.listdir(IMAGES_INPUT_FOLDER)
    pred_files = os.listdir(PREDICTIONS_INPUT_FOLDER)
    class_color_map = {}
    overlap_report = []

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    for image_file in sorted(image_files):
        if image_file.startswith('.'):
            continue

        pred_file = str(Path(image_file).with_suffix(".json"))
        if pred_file not in pred_files:
            print(f"Skipping {image_file}, corresponding predictions JSON file not found.")
            continue

        full_img_path = os.path.join(IMAGES_INPUT_FOLDER, image_file)
        full_json_predictions_path = os.path.join(PREDICTIONS_INPUT_FOLDER, pred_file)
        full_output_path = os.path.join(OUTPUT_FOLDER, image_file)

        overlaps = draw_predictions(full_img_path, full_json_predictions_path, full_output_path, class_color_map)
        if overlaps:
            overlap_report.append((image_file, overlaps))

    # Print overlap summary
    if overlap_report:
        print("\n" + "=" * 60)
        print("OVERLAP REPORT (IoU >= 0.5)")
        print("=" * 60)
        for image_file, overlaps in overlap_report:
            print(f"\n  {image_file}:")
            for cls_a, conf_a, cls_b, conf_b, iou in overlaps:
                print(f"    - {cls_a} ({conf_a:.2f}) <-> {cls_b} ({conf_b:.2f})  IoU={iou:.2f}")
        print(f"\nTotal images with overlaps: {len(overlap_report)}")
    else:
        print("\nNo significant overlaps detected.")

    # Print colour legend
    if class_color_map:
        print("\n" + "-" * 40)
        print("CLASS COLOUR LEGEND (BGR):")
        for cls, color in class_color_map.items():
            print(f"  {cls}: {color}")

if __name__ == "__main__":
    main()