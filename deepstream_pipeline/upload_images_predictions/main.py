import os
import json
import uuid
from google.cloud import storage, bigquery
from datetime import datetime
import shutil
import time
import threading
 
# -----------------------------
# CONFIGURATION
# -----------------------------
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/reply/frosta/edge/service_account.json"
GCP_PROJECT = "oed-fro-harvest-cam-prd-2e26"
GCS_BUCKET_NAME = "oed-fro-harvest-cam-prd-2e26-raw_images"
 
BASE_DIR = "/home/reply/DeepStream-Yolo-frosta/deepstream_pipeline"
IMAGES_BASE = os.path.join(BASE_DIR, "images")
PREDICTIONS_BASE = os.path.join(BASE_DIR, "predictions")
 
production_collect = True
 
BQ_DATASET = "Retraining_test"
BQ_TABLE_COORDINATES = "PredictionsCoordinates"
BQ_TABLE_IMAGE_INFO = "ImageInfo"
 
storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET_NAME)
bq_client = bigquery.Client()
coordinates_table = bq_client.dataset(BQ_DATASET).table(BQ_TABLE_COORDINATES)
image_info_table = bq_client.dataset(BQ_DATASET).table(BQ_TABLE_IMAGE_INFO)

SESSION_CACHE = {}            # Stores (root_path, recording_session_id) tuples
cache_lock = threading.Lock() # The single key that prevents threads colliding
 
 
# -----------------------------
# HELPERS
# -----------------------------
 
def folder_is_empty_and_stable(folder, wait_seconds=2):
    """Returns True only if folder remains empty after waiting."""
    before = os.listdir(folder)
    if before:
        return False
    time.sleep(wait_seconds)
    after = os.listdir(folder)
    return len(after) == 0
 
 
def clear_folder(folder):
    """Delete only contents of folder, not the folder itself."""
    for item in os.listdir(folder):
        path = os.path.join(folder, item)
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
 
 
# -----------------------------
# BIGQUERY + GCS
# -----------------------------
 
def get_recording_session_info(production_collect: bool, crop_type: str, filling_line: str, recording_session_id: str):
    table = f"{GCP_PROJECT}.{BQ_DATASET}.ImageRecordingSession"
 
    if production_collect:
        now = datetime.utcnow()
        current_year = now.strftime("%Y")          
        current_month_num = now.strftime("%m")     
        current_month_name = now.strftime("%B").lower()
        recording_session_id = f"{current_year}_{current_month_num}_{current_month_name}_prod_coll_{crop_type}" #Structure done for readability and sorting easily
    # 1. Grab the lock & instantly check memory 
    with cache_lock:
        if recording_session_id in SESSION_CACHE:
            return SESSION_CACHE[recording_session_id] 
 
    query = f"""
        SELECT rootPath
        FROM `{table}`
        WHERE id = @recording_session_id
        LIMIT 1
    """
 
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("recording_session_id", "STRING", recording_session_id)
        ]
    )
 
    rows = [dict(row) for row in bq_client.query(query, job_config=job_config).result()]
    if rows:
        root_path = rows[0]["rootPath"]
 
    else:
        root_path = f"recording_session/{recording_session_id}"
    
        insert_query = f"""
            INSERT INTO `{table}` (id, cropType, rootPath, startedAt, bucketPrefix, bucket_name, filling_line)
            VALUES (@id, @cropType, @rootPath, CURRENT_TIMESTAMP(), @bucketPrefix, @bucket_name, @filling_line)
        """
    
        insert_job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("id", "STRING", recording_session_id),
                bigquery.ScalarQueryParameter("cropType", "STRING", crop_type),
                bigquery.ScalarQueryParameter("rootPath", "STRING", root_path),
                bigquery.ScalarQueryParameter("bucketPrefix", "STRING", f"{GCS_BUCKET_NAME}/{root_path}"),
                bigquery.ScalarQueryParameter("bucket_name", "STRING", GCS_BUCKET_NAME),
                bigquery.ScalarQueryParameter("filling_line", "STRING", filling_line)
            ]
        )
    
        bq_client.query(insert_query, job_config=insert_job_config).result()
        print(f"Created new recording session: {recording_session_id}")
    #Quickly lock again just to save the result for next time
    with cache_lock:
        SESSION_CACHE[recording_session_id] = (root_path, recording_session_id)

    return root_path, recording_session_id
 
 
def upload_image_to_gcs(local_path, gcs_path):
    if gcs_path.startswith("gs://"):
        gcs_path = "/".join(gcs_path.split("/")[1:])
    blob = bucket.blob(gcs_path)
    blob.upload_from_filename(local_path)
    print(f"📤 Uploaded image {local_path} → {gcs_path}")
 
 
def upload_prediction_to_bigquery(image_name, prediction_path, session_id):
    try:
        with open(prediction_path, "r") as f:
            prediction_json = json.load(f)
 
        row_coordinates = {
            "id": str(uuid.uuid4()),
            "image_info_name": image_name,
            "session_id": session_id,
            "predictions": json.dumps(prediction_json)
        }
 
        row_image_info = {
            "img_name": image_name,
            "selected": False,
            "session": session_id
        }
 
        bq_client.insert_rows_json(coordinates_table, [row_coordinates])
        bq_client.insert_rows_json(image_info_table, [row_image_info])
        print(f"Prediction uploaded for: {image_name} in session id: {session_id}")
 
    except Exception as e:
        print(f"❌ Prediction upload failed for {image_name}: {e}")
 
 
# -----------------------------
# MAIN SCANNING LOOP
# -----------------------------
 
def scan_new_images(mode: str):
 
    images_path = os.path.join(IMAGES_BASE, mode)
    predictions_path = os.path.join(PREDICTIONS_BASE, mode)
    
 
    while True:
 
        if not os.path.exists(images_path):
            print(f"❌ Images path missing: {images_path}")
            time.sleep(1)
            continue
 
        filling_lines = [
            d for d in os.listdir(images_path)
            if os.path.isdir(os.path.join(images_path, d))
        ]
 
        # Folder may be empty for a few seconds while DeepStream starts
        if not filling_lines:
            if folder_is_empty_and_stable(images_path):
                print("⏳ No filling lines yet… waiting.")
                time.sleep(1)
                continue
 
        for filling_line in filling_lines:
 
            images_folder = os.path.join(images_path, filling_line)
            predictions_folder = os.path.join(predictions_path, filling_line) if os.path.exists(predictions_path) else None
 
            print(f"🔄 Processing filling line: {filling_line}")
            # saving using bmp convention. add .bmp to the list of extensions to check for
            files = [f for f in os.listdir(images_folder) if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))]
 
            # If empty, check again later
            if not files:
                if folder_is_empty_and_stable(images_folder):
                    time.sleep(1)
                continue
 
            # --------------- PROCESS IMAGES ---------------
            for filename in files:
 
                image_path = os.path.join(images_folder, filename)
                crop_type = filename.split("_")[0]
 
                prefix, session_id = get_recording_session_info(
                    production_collect=True,
                    crop_type=crop_type,
                    filling_line=filling_line,
                    recording_session_id=None
                )
 
                gcs_path = os.path.join(prefix, filename)
                upload_image_to_gcs(image_path, gcs_path)
 
                # Upload prediction
                if predictions_folder:
                    pred_path = os.path.join(predictions_folder, os.path.splitext(filename)[0] + ".json")
                    if os.path.exists(pred_path):
                        upload_prediction_to_bigquery(filename, pred_path, session_id)
 
                # DELETE IMAGE AFTER PROCESSING
                try:
                    os.remove(image_path)
                    print(f"Image {image_path} was removed")
                except:
                    pass
 
            # Clear predictions once all processed
            if predictions_folder:
                clear_folder(predictions_folder)
 
        #time.sleep(0.5) Maybe return to this later
 
 
# -----------------------------
# ENTRYPOINT
# -----------------------------
def main():
    threading.Thread(target=scan_new_images, args=("production",), daemon=True).start()
    #threading.Thread(target=scan_new_images, args=("manual",), daemon=True).start()
 
    while True:
        time.sleep(1)
 
 
if __name__ == "__main__":
    main()