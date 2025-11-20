from flask import Blueprint, request, jsonify
import os, shutil
from decimal import Decimal
from ultralytics import YOLO 
from collections import Counter
from datetime import datetime
from utils.database import get_dict_cursor_connection

mobile_bp = Blueprint('mobile', __name__)
    
# 실제 저장 경로
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE_DIR, "..", "models")
UPLOAD_DIR = os.path.join(BASE_DIR, "..", "static", "uploads", "crop_images")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ripe_model = None
rotten_model = None

# 🔥 YOLO 모델 로드
def load_models():
    global ripe_model, rotten_model
    if ripe_model is None or rotten_model is None:
        try:
            from ultralytics import YOLO
            ripe_model = YOLO(os.path.join(MODEL_DIR, "ripe_straw.pt"))
            rotten_model = YOLO(os.path.join(MODEL_DIR, "rotten_straw.pt"))
            print("✅ YOLO 모델 로딩 완료 (lazy load)")
        except Exception as e:
            print(f"❌ YOLO 로딩 실패: {e}")
            ripe_model = None
            rotten_model = None


@mobile_bp.route("/predict", methods=["POST"])
def predict():
    """Flutter → 서버로 이미지 업로드 및 YOLO 추론"""

    load_models()

    if ripe_model is None or rotten_model is None:
        return jsonify({"error": "YOLO 모델을 불러올 수 없습니다."}), 500

    if 'file' not in request.files:
        return jsonify({"error": "파일이 없습니다."}), 400

    file = request.files['file']
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    file.save(file_path)

    print(f"📸 받은 파일: {file.filename}")
    print(f"📁 저장 경로: {file_path}")

    # YOLO 추론 수행
    ripe_result = ripe_model(file_path, conf=0.25)
    rotten_result = rotten_model(file_path, conf=0.25)

    print("🔥 Ripe boxes:", ripe_result[0].boxes)
    print("🔥 Ripe classes:", ripe_result[0].boxes.cls)

    # 라벨 리스트 추출
    ripe_labels = [ripe_model.names[int(cls)] for cls in ripe_result[0].boxes.cls]
    rotten_labels = [rotten_model.names[int(cls)] for cls in rotten_result[0].boxes.cls]

    # 카운팅
    count_ripe = Counter(ripe_labels)       # straw-ripe, straw-unripe
    count_rotten = Counter(rotten_labels)   # starw_rotten, strwa_healthy

    # 웹 구조와 동일하게 매핑
    ripe = count_ripe.get("straw-ripe", 0)
    unripe = count_ripe.get("straw-unripe", 0)

    healthy = count_rotten.get("strwa_healthy", 0)      # 정상 딸기
    rotten = count_rotten.get("starw_rotten", 0) > 0

    if (ripe + unripe) > 0:
        total = ripe + unripe
    else:
        total = healthy


    # 성숙도 계산 (웹과 동일)
    if total > 0:
        ripeness_percent = round((ripe / total) * 100, 1)
    else:
        ripeness_percent = 0

    print("\n=== YOLO 추론 결과 ===")
    print(f"익은(straw-ripe): {ripe}")
    print(f"덜 익은(straw-unripe): {unripe}")
    print(f"건강한(strwa_healthy): {healthy}")
    print(f"썩은(starw_rotten): {rotten}")
    print(f"전체 딸기: {total}")
    print(f"익은 비율: {ripeness_percent}%")
    print("========================\n")

    # Flutter 응답
    return jsonify({
        "status": "success",
        "filename": file.filename,
        "ripe": ripe,
        "unripe": unripe,
        "rotten": rotten,                # boolean
        "total": total,
        "ripeness_percent": ripeness_percent
    })

@mobile_bp.route("/ping")
def ping():
    return "ok"

# 농장 통계 API
@mobile_bp.route("/api/farms/status", methods=["GET"])
def farms_status():
    user_id = request.args.get("user_id")
    conn, cursor = get_dict_cursor_connection()
    if not conn or not cursor:
        return jsonify({"error": "DB 연결 실패"}), 500

    cursor.execute("""
        SELECT 
            f.id AS farm_id,
            f.name AS farm_name,
            COALESCE(ROUND(SUM(cg.harvest_amount)::numeric / NULLIF(SUM(NULLIF(cg.total_amount, 0))::numeric, 0) * 100, 1), 0) AS ripeness
        FROM farms f
        LEFT JOIN greenhouses g ON g.farm_id = f.id
        LEFT JOIN crop_groups cg ON cg.greenhouse_id = g.id
        WHERE f.owner_username = %s
        GROUP BY f.id, f.name;
    """, (user_id,))
    result = cursor.fetchall()

    cursor.close()
    conn.close()

    for row in result:
        for k, v in row.items():
            if isinstance(v, Decimal):
                row[k] = float(v)

    return jsonify(result)

# FCM 토큰 저장
@mobile_bp.route("/save-token", methods=["POST"])
def save_token():
    data = request.get_json()
    user_id = data.get("user_id")
    fcm_token = data.get("fcm_token")

    if not user_id or not fcm_token:
        return jsonify({
            "success": False,
            "message": "user_id와 fcm_token이 필요합니다."
        }), 400

    conn, cursor = get_dict_cursor_connection()
    if not conn or not cursor:
        return jsonify({"error": "DB 연결 실패"}), 500

    try:
        cursor.execute("""
            UPDATE users SET fcm_token=%s WHERE id=%s
        """, (fcm_token, user_id))
        conn.commit()
        print(f"✅ {user_id}의 FCM 토큰 저장 완료: {fcm_token[:30]}...")
        return jsonify({"success": True, "message": "토큰 저장 성공"})

    except Exception as e:
        conn.rollback()
        print(f"❌ FCM 토큰 저장 중 오류: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

# 알림 리스트 조회
@mobile_bp.route("/api/notifications/<user_id>", methods=["GET"])
def get_notifications(user_id):
    conn, cursor = get_dict_cursor_connection()
    if not conn or not cursor:
        return jsonify({"error": "DB 연결 실패"}), 500

    try:
        cursor.execute("""
            SELECT 
                id, 
                message, 
                type, 
                image_url, 
                created_at, 
                is_read
            FROM notification
            WHERE receiver_id = %s
            ORDER BY created_at DESC
        """, (user_id,))
        result = cursor.fetchall()

        for row in result:
            for k, v in row.items():
                if isinstance(v, datetime):
                    row[k] = v.strftime("%Y-%m-%d %H:%M:%S")

        return jsonify(result)

    except Exception as e:
        print(f"❌ 알림 조회 중 오류: {e}")
        return jsonify({"error": str(e)}), 500

    finally:
        cursor.close()
        conn.close()

@mobile_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    user_id = data.get("id")
    password = data.get("password")

    if not user_id or not password:
        return jsonify({"success": False, "message": "ID와 비밀번호를 입력해주세요."}), 400

    conn, cursor = get_dict_cursor_connection()
    if not conn or not cursor:
        return jsonify({"success": False, "message": "DB 연결 실패"}), 500

    try:
        cursor.execute("""
            SELECT id, password, nickname
            FROM users
            WHERE id = %s
        """, (user_id,))
        user = cursor.fetchone()

        if not user:
            return jsonify({"success": False, "message": "존재하지 않는 사용자입니다."}), 404

        if user["password"] != password:
            return jsonify({"success": False, "message": "비밀번호가 일치하지 않습니다."}), 401

        return jsonify({
            "success": True,
            "message": "로그인 성공",
            "user_id": user["id"],
            "nickname": user["nickname"]
        }), 200

    except Exception as e:
        print(f"❌ 로그인 중 오류: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

    finally:
        cursor.close()
        conn.close()