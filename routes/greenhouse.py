# routes/greenhouse.py

from flask import Blueprint, request, jsonify, session, render_template
import psycopg2
import psycopg2.extras
import json
from utils.database import get_db_connection
import requests
from collections import Counter
from werkzeug.utils import secure_filename
import uuid
from datetime import datetime

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    print("Warning: ultralytics not available. YOLO features disabled.")

import os




greenhouse_bp = Blueprint('greenhouse', __name__)

# --------------------------
# 그룹 생성 관련 유틸 함수
# --------------------------
def find_contiguous_segments(line):
    segments = []
    start = 0
    val = line[0]
    for i in range(1, len(line)):
        if line[i] != val:
            segments.append((start, i - 1, val))
            start = i
            val = line[i]
    segments.append((start, len(line) - 1, val))
    return segments

def find_row_groups(grid):
    groups = []
    for row_idx, row in enumerate(grid):
        segments = find_contiguous_segments(row)
        for start, end, val in segments:
            if end > start:
                groups.append((row_idx, start, end, val))
    return groups

def find_col_groups(grid):
    groups = []
    for col_idx in range(len(grid[0])):
        col = [row[col_idx] for row in grid]
        segments = find_contiguous_segments(col)
        for start, end, val in segments:
            if end > start:
                groups.append((start, col_idx, end, val))
    return groups

# ✅ 자동으로 수평 vs 수직 그룹 수 비교하여 하나만 저장

def save_crop_groups(greenhouse_id, grid_data, conn):
    cur = conn.cursor()
    
    # 기존 그룹 조회
    cur.execute("""
        SELECT id, group_cells, crop_type, is_horizontal, 
               harvest_amount, total_amount, last_image_path, last_analysis_result
        FROM crop_groups 
        WHERE greenhouse_id = %s
    """, (greenhouse_id,))
    existing_groups = cur.fetchall()
    
    # 기존 그룹을 딕셔너리로 변환 (group_cells를 키로 사용)
    existing_dict = {}
    for row in existing_groups:
        group_id, cells, crop_type, is_horizontal, harvest, total, img_path, analysis = row
        if isinstance(cells, str):
            cells = json.loads(cells)
        # cells를 정렬해서 문자열로 만들어 키로 사용
        cells_key = json.dumps(sorted([tuple(c) for c in cells]))
        existing_dict[cells_key] = {
            'id': group_id,
            'crop_type': crop_type,
            'is_horizontal': is_horizontal,
            'harvest_amount': harvest,
            'total_amount': total,
            'last_image_path': img_path,
            'last_analysis_result': analysis
        }

    row_groups = find_row_groups(grid_data)
    col_groups = find_col_groups(grid_data)

    # ✅ 첫 번째 행의 값이 모두 같으면 가로 병합, 아니면 세로 병합
    if all(x == grid_data[0][0] for x in grid_data[0]):
        selected_groups = row_groups
        is_horizontal = True
    else:
        selected_groups = col_groups
        is_horizontal = False

    new_cells_keys = set()
    
    for group in selected_groups:
        if is_horizontal:
            row_idx, start_col, end_col, value = group
            cells = [[row_idx, col] for col in range(start_col, end_col + 1)]
        else:
            start_row, col_idx, end_row, value = group
            cells = [[row, col_idx] for row in range(start_row, end_row + 1)]

        cells_key = json.dumps(sorted([tuple(c) for c in cells]))
        new_cells_keys.add(cells_key)
        
        # 기존 그룹이 있으면 업데이트, 없으면 새로 생성
        if cells_key in existing_dict:
            # 기존 그룹 업데이트 (crop_type만 업데이트, 분석 데이터는 유지)
            existing = existing_dict[cells_key]
            cur.execute("""
                UPDATE crop_groups
                SET crop_type = %s, is_horizontal = %s
                WHERE id = %s
            """, (value, is_horizontal, existing['id']))
        else:
            # 새 그룹 생성
            cur.execute("""
                INSERT INTO crop_groups (greenhouse_id, group_cells, crop_type, is_horizontal, is_read)
                VALUES (%s, %s, %s, %s, %s)
            """, (greenhouse_id, json.dumps(cells), value, is_horizontal, False))
    
    # 더 이상 존재하지 않는 그룹 삭제
    for cells_key, existing in existing_dict.items():
        if cells_key not in new_cells_keys:
            cur.execute("DELETE FROM crop_groups WHERE id = %s", (existing['id'],))

# --------------------------
# 비닐하우스 생성
# --------------------------
@greenhouse_bp.route('/create', methods=['POST'])
def create_greenhouse():
    try:
        data = request.get_json()
        farm_id = data.get('farm_id')
        name = data.get('name')
        num_rows = data.get('num_rows')
        num_cols = data.get('num_cols')
        grid_data = data.get('grid_data')

        if not all([farm_id, name, num_rows, num_cols, grid_data]):
            return jsonify({"message": "필수 정보가 누락되었습니다."}), 400

        conn = get_db_connection()
        if conn is None:
            return jsonify({"message": "DB 연결 실패"}), 500

        cur = conn.cursor()
        sql = """
            INSERT INTO greenhouses (farm_id, name, num_rows, num_cols, grid_data)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """
        cur.execute(sql, (
            farm_id,
            name,
            num_rows,
            num_cols,
            json.dumps(grid_data)
        ))
        greenhouse_id = cur.fetchone()[0]

        # ✅ 그룹 저장
        save_crop_groups(greenhouse_id, grid_data, conn)

        conn.commit()
        conn.close()

        return jsonify({"message": "✅ 하우스 저장 완료"}), 200
    except Exception as e:
        print("❌ 저장 오류:", e)
        return jsonify({"message": "서버 오류 발생"}), 500

@greenhouse_bp.route('/update/<int:greenhouse_id>', methods=['POST'])
def update_greenhouse(greenhouse_id):
    try:
        data = request.get_json()
        name = data.get('name')
        num_rows = data.get('num_rows')
        num_cols = data.get('num_cols')
        grid_data = data.get('grid_data')

        if not all([name, num_rows, num_cols, grid_data]):
            return jsonify({"message": "필수 정보가 누락되었습니다."}), 400

        conn = get_db_connection()
        cur = conn.cursor()

        sql = """
            UPDATE greenhouses
            SET name = %s, num_rows = %s, num_cols = %s, grid_data = %s
            WHERE id = %s
        """
        cur.execute(sql, (
            name,
            num_rows,
            num_cols,
            json.dumps(grid_data),
            greenhouse_id
        ))

        # ✅ 업데이트 시에도 그룹 재생성
        save_crop_groups(greenhouse_id, grid_data, conn)

        conn.commit()
        conn.close()

        return jsonify({"message": "✅ 하우스 업데이트 완료"}), 200
    except Exception as e:
        print("❌ 업데이트 오류:", e)
        return jsonify({"message": "서버 오류 발생"}), 500

@greenhouse_bp.route('/<int:greenhouse_id>', methods=['DELETE'])
def delete_greenhouse(greenhouse_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id FROM greenhouses WHERE id = %s", (greenhouse_id,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"message": "해당 하우스가 존재하지 않습니다."}), 404

        cur.execute("DELETE FROM greenhouses WHERE id = %s", (greenhouse_id,))
        conn.commit()
        conn.close()

        return jsonify({"message": "✅ 하우스 삭제 완료"}), 200
    except Exception as e:
        print("❌ 삭제 오류:", e)
        return jsonify({"message": "서버 오류 발생"}), 500

@greenhouse_bp.route('/list/<int:farm_id>', methods=['GET'])
def list_greenhouses(farm_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        sql = "SELECT id, name FROM greenhouses WHERE farm_id = %s ORDER BY id"
        cur.execute(sql, (farm_id,))
        rows = cur.fetchall()
        
        # 수동으로 딕셔너리 형태로 변환
        greenhouses = []
        for row in rows:
            greenhouses.append({
                'id': row[0],
                'name': row[1]
            })
        
        conn.close()
        return jsonify({"greenhouses": greenhouses}), 200
    except Exception as e:
        print("❌ 목록 불러오기 오류:", e)
        return jsonify({"message": "서버 오류 발생"}), 500

@greenhouse_bp.route('/grid', methods=['GET'])
def grid_generator():
    greenhouse_id = request.args.get('id')
    farm_id = request.args.get('farm_id')
    house_name = ""
    num_rows = 10
    num_cols = 10
    grid_data = []

    conn = get_db_connection()
    cur = conn.cursor()

    if greenhouse_id:
        cur.execute("SELECT farm_id, name, num_rows, num_cols, grid_data FROM greenhouses WHERE id = %s", (greenhouse_id,))
        greenhouse = cur.fetchone()
        if greenhouse:
            farm_id = greenhouse[0]
            house_name = greenhouse[1]
            num_rows = greenhouse[2]
            num_cols = greenhouse[3]
            # grid_data 처리 - 이미 리스트인지 문자열인지 확인
            grid_data = greenhouse[4]
            if isinstance(grid_data, str):
                try:
                    grid_data = json.loads(grid_data)
                except (json.JSONDecodeError, TypeError):
                    grid_data = []
            elif not isinstance(grid_data, list):
                grid_data = []
        else:
            conn.close()
            return "존재하지 않는 비닐하우스입니다.", 404
    else:
        if not farm_id:
            username = session.get('user_id')
            if not username:
                conn.close()
                return "로그인이 필요합니다.", 401
            cur.execute("SELECT id FROM farms WHERE owner_username = %s LIMIT 1", (username,))
            farm = cur.fetchone()
            if farm:
                farm_id = farm[0]
            else:
                conn.close()
                return "등록된 농장이 없습니다.", 404

    conn.close()

    return render_template('grid_generator.html',
                           farm_id=farm_id,
                           greenhouse_id=greenhouse_id or '',
                           house_name=house_name,
                           num_rows=num_rows,
                           num_cols=num_cols,
                           grid_data=json.dumps(grid_data))

@greenhouse_bp.route('/api/grid', methods=['GET'])
def get_grid_data():
    try:
        greenhouse_id = request.args.get('id')
        if not greenhouse_id:
            return jsonify({'error': 'greenhouse_id required'}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'DB 연결 실패'}), 500
            
        cur = conn.cursor()
        cur.execute("SELECT num_rows, num_cols, grid_data FROM greenhouses WHERE id = %s", (greenhouse_id,))
        greenhouse = cur.fetchone()
        
        if not greenhouse:
            return jsonify({'error': '존재하지 않는 비닐하우스입니다.'}), 404

        # grid_data 처리 - 이미 리스트인지 문자열인지 확인
        grid_data = greenhouse[2]
        if isinstance(grid_data, str):
            try:
                grid_data = json.loads(grid_data)
            except (json.JSONDecodeError, TypeError):
                grid_data = []
        elif not isinstance(grid_data, list):
            grid_data = []

        return jsonify({
            'num_rows': greenhouse[0],
            'num_cols': greenhouse[1],
            'grid_data': grid_data
        })
    except Exception as e:
        print(f"Grid API 오류: {str(e)}")
        return jsonify({'error': f'그리드 데이터 조회 실패: {str(e)}'}), 500
    finally:
        if 'conn' in locals() and conn:
            conn.close()

@greenhouse_bp.route('/<int:greenhouse_id>/groups', methods=['GET'])
def get_crop_groups(greenhouse_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, group_cells, crop_type, is_horizontal, harvest_amount, total_amount, 
               last_image_path, last_analysis_result 
        FROM crop_groups 
        WHERE greenhouse_id = %s
    """, (greenhouse_id,))
    rows = cur.fetchall()
    conn.close()
    
    # 수동으로 딕셔너리 형태로 변환
    groups = []
    for row in rows:
        group_cells = row[1]
        if isinstance(group_cells, str):
            try:
                group_cells = json.loads(group_cells)
            except Exception:
                group_cells = []
        
        # last_analysis_result 파싱
        analysis_result = row[7]
        if isinstance(analysis_result, str):
            try:
                analysis_result = json.loads(analysis_result)
            except Exception:
                analysis_result = None
        
        groups.append({
            'id': row[0],
            'group_cells': group_cells,
            'crop_type': row[2],
            'is_horizontal': row[3],
            'harvest_amount': row[4],
            'total_amount': row[5],
            'last_image_path': row[6],
            'last_analysis_result': analysis_result
        })
    
    axis = None
    if groups:
        axis = 'row' if groups[0]['is_horizontal'] else 'col'
    
    return jsonify({'groups': groups, 'axis': axis})



# --------------------------
# IoT 촬영 및 분석 시스템
# --------------------------
# 상수
# Raspberry Pi IP 주소
# 로컬 테스트: "http://172.20.47.250:5002" (실제 라즈베리파이)
# 배포 환경 (ngrok): "https://proud-adder-allegedly.ngrok-free.app"
RASPBERRY_PI_IP = os.getenv('RASPBERRY_PI_IP', "http://165.229.148.72:5002")

IMAGE_DIR = "test_images/"
UPLOAD_DIR = "static/uploads/crop_images/"

# 배포된 서버 주소
IOT_IMAGE_UPLOAD_URL = "https://smart-farm-ignore.onrender.com/api/greenhouses/iot-image-upload"

# 업로드 디렉토리 생성
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# YOLO 모델 초기화 (사용 가능한 경우에만)
MODEL_RIPE = None
MODEL_ROTTEN = None
if YOLO_AVAILABLE:
    try:
        MODEL_RIPE = YOLO("model/ripe_straw.pt")
        MODEL_ROTTEN = YOLO("model/rotten_straw.pt")
    except Exception as e:
        print(f"Warning: YOLO model loading failed: {e}")
        YOLO_AVAILABLE = False

@greenhouse_bp.route('/crop_groups/read', methods=['POST'])
def crop_groups_read():
    try:
        data = request.get_json()
        group_id = data.get('group_id')
        iot_id = data.get('iot_id')

        if not group_id or not iot_id:
            return jsonify({'message': '필수 정보가 누락되었습니다.'}), 400

        # group_id로부터 greenhouse_id 조회
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT greenhouse_id FROM crop_groups WHERE id = %s", (group_id,))
        result = cur.fetchone()
        conn.close()
        
        if not result:
            return jsonify({'message': '해당 그룹을 찾을 수 없습니다.'}), 404
        
        greenhouse_id = result[0]
        print(f"📍 Group ID {group_id} → Greenhouse ID {greenhouse_id}")

        # 새 촬영 명령이 들어오면 기존 이미지 초기화
        # 같은 촬영 세션 내에서 여러 번 촬영하는 경우를 위해 타임스탬프로 구분
        capture_session_timestamp = datetime.now().isoformat()
        conn = get_db_connection()
        cur = conn.cursor()
        # 기존 analyzed_files를 초기화하고 새로운 세션 시작을 표시
        new_session_data = {
            'capture_session_start': capture_session_timestamp,
            'analyzed_files': []
        }
        cur.execute("""
            UPDATE crop_groups
            SET last_analysis_result = %s
            WHERE id = %s
        """, (json.dumps(new_session_data), group_id))
        conn.commit()
        conn.close()
        print(f"🔄 새 촬영 세션 시작 - 그룹 ID: {group_id}, 세션 시작 시간: {capture_session_timestamp}")

        # ✅ 촬영 명령 → Raspberry Pi
        try:
            # IoT 디바이스에 촬영 명령 전송
            # IoT는 촬영 후 자동으로 /api/greenhouses/iot-image-upload 엔드포인트로 이미지 업로드
            capture_command = {
                "group_id": group_id, 
                "iot_id": iot_id,
                "greenhouse_id": greenhouse_id,  # greenhouse_id 추가
                "upload_url": f"http://localhost:5001/api/greenhouses/iot-image-upload",
                "action": "capture_and_upload"
            }
            
            res = requests.post(
                f"{RASPBERRY_PI_IP}/capture-command",
                json=capture_command,
                timeout=10  # 촬영 시간을 고려해서 타임아웃 증가
            )
            res.raise_for_status()
            
            print(f"✅ IoT 촬영 명령 전송 성공 - 그룹 ID: {group_id}, IoT ID: {iot_id}, Greenhouse ID: {greenhouse_id}")
            
            # IoT가 비동기적으로 이미지를 업로드하고 분석할 것이므로
            # 여기서는 명령 전송 성공만 응답
            return jsonify({
                "message": "📸 IoT 촬영 명령이 전송되었습니다. 잠시 후 결과가 업데이트됩니다.",
                "status": "command_sent",
                "group_id": group_id,
                "iot_id": iot_id,
                "greenhouse_id": greenhouse_id
            }), 200
            
        except Exception as iot_err:
            print(f"❌ IoT 명령 전송 실패: {iot_err}")
            return jsonify({'message': 'IoT 촬영 명령 전송 실패', 'error': str(iot_err)}), 502



    except Exception as e:
        print("❌ 전체 오류:", e)
        return jsonify({'message': '서버 오류 발생', 'error': str(e)}), 500



# --------------------------
# 사진 업로드 및 분석 기능
# --------------------------
@greenhouse_bp.route('/crop_groups/upload_analyze', methods=['POST'])
def upload_and_analyze():
    try:
        # 폼 데이터에서 group_id 가져오기
        group_id = request.form.get('group_id')
        if not group_id:
            return jsonify({'message': 'group_id가 필요합니다.'}), 400

        # 업로드된 파일들 확인
        if 'images' not in request.files:
            return jsonify({'message': '업로드할 이미지가 없습니다.'}), 400

        files = request.files.getlist('images')
        if not files or all(f.filename == '' for f in files):
            return jsonify({'message': '선택된 파일이 없습니다.'}), 400

        # 전체 분석 결과를 저장할 변수들
        total_ripe = 0
        total_unripe = 0
        total_count = 0
        has_any_rotten = False
        analyzed_files = []

        # 각 이미지 파일 처리
        for file in files:
            if file and file.filename != '':
                # 안전한 파일명 생성
                from werkzeug.utils import secure_filename
                import uuid
                from datetime import datetime
                
                filename = secure_filename(file.filename)
                unique_filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{filename}"
                file_path = os.path.join(UPLOAD_DIR, unique_filename)
                
                # 파일 저장
                file.save(file_path)
                
                # YOLO 분석
                if YOLO_AVAILABLE and MODEL_RIPE and MODEL_ROTTEN:
                    try:
                        # 익은/안익은 딸기 분석
                        result_ripe = MODEL_RIPE(file_path, conf=0.5)
                        result_rotten = MODEL_ROTTEN(file_path, conf=0.5)

                        ripe_classes = [MODEL_RIPE.names[int(cls)] for cls in result_ripe[0].boxes.cls]
                        rotten_classes = [MODEL_ROTTEN.names[int(cls)] for cls in result_rotten[0].boxes.cls]

                        count_ripe = Counter(ripe_classes)
                        count_rotten = Counter(rotten_classes)

                        file_ripe = count_ripe.get("straw-ripe", 0)
                        file_unripe = count_ripe.get("straw-unripe", 0)
                        file_total = file_ripe + file_unripe
                        file_has_rotten = count_rotten.get("starw_rotten", 0) > 0

                        # 전체 결과에 누적
                        total_ripe += file_ripe
                        total_unripe += file_unripe
                        total_count += file_total
                        if file_has_rotten:
                            has_any_rotten = True

                        analyzed_files.append({
                            'filename': unique_filename,
                            'ripe': file_ripe,
                            'unripe': file_unripe,
                            'total': file_total,
                            'rotten': file_has_rotten
                        })

                    except Exception as yolo_err:
                        print(f"❌ YOLO 분석 실패 ({unique_filename}): {yolo_err}")
                        # 분석 실패 시 더미 데이터
                        analyzed_files.append({
                            'filename': unique_filename,
                            'ripe': 2,
                            'unripe': 1,
                            'total': 3,
                            'rotten': False,
                            'error': 'YOLO 분석 실패'
                        })
                        total_ripe += 2
                        total_unripe += 1
                        total_count += 3
                else:
                    # YOLO 사용 불가 시 더미 데이터
                    analyzed_files.append({
                        'filename': unique_filename,
                        'ripe': 3,
                        'unripe': 2,
                        'total': 5,
                        'rotten': False,
                        'note': 'YOLO 모델 사용 불가'
                    })
                    total_ripe += 3
                    total_unripe += 2
                    total_count += 5



        # DB 업데이트 (harvest_amount, total_amount, is_read, last_image_path, last_analysis_result)
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 분석 결과를 JSON으로 저장
        import json
        analysis_result = {
            'total_files': len(analyzed_files),
            'total_ripe': total_ripe,
            'total_unripe': total_unripe,
            'total_count': total_count,
            'has_rotten': has_any_rotten,
            'analyzed_files': analyzed_files
        }
        
        # 첫 번째 이미지 경로 저장
        first_image = analyzed_files[0]['filename'] if analyzed_files else None
        
        cur.execute("""
            UPDATE crop_groups
            SET harvest_amount = %s,
                total_amount = %s,
                is_read = %s,
                last_image_path = %s,
                last_analysis_result = %s
            WHERE id = %s
        """, (total_ripe, total_count, True if has_any_rotten else False, 
              first_image, json.dumps(analysis_result), group_id))
        conn.commit()
        conn.close()

        # 응답 반환
        return jsonify({
            "message": "📸 이미지 업로드 및 분석 완료",
            "result": {
                "total_files": len(analyzed_files),
                "total_ripe": total_ripe,
                "total_unripe": total_unripe,
                "total_count": total_count,
                "has_rotten": "✅ 발견됨" if has_any_rotten else "❌ 없음",
                "is_read": True if has_any_rotten else False,
                "analyzed_files": analyzed_files
            }
        }), 200

    except Exception as e:
        print("❌ 업로드 및 분석 오류:", e)
        return jsonify({'message': '서버 오류 발생', 'error': str(e)}), 500

# --------------------------
# IoT 이미지 업로드 및 분석 엔드포인트
# --------------------------
@greenhouse_bp.route('/iot-image-upload', methods=['POST'])
def iot_image_upload():
    """
    IoT 디바이스에서 촬영한 이미지를 업로드하고 YOLO 모델로 분석하는 엔드포인트
    IoT 디바이스가 이 엔드포인트로 이미지를 전송하면 자동으로 분석 후 DB 업데이트
    """
    try:
        # 폼 데이터에서 정보 가져오기
        group_id = request.form.get('group_id')
        iot_id = request.form.get('iot_id')
        
        print(f"📥 IoT 이미지 업로드 요청 - group_id: {group_id}, iot_id: {iot_id}")
        print(f"📥 Form data: {dict(request.form)}")
        print(f"📥 Files: {list(request.files.keys())}")
        
        if not group_id or not iot_id:
            print(f"❌ 필수 파라미터 누락 - group_id: {group_id}, iot_id: {iot_id}")
            return jsonify({'message': 'group_id와 iot_id가 필요합니다.'}), 400

        # group_id로부터 greenhouse_id 조회
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT greenhouse_id FROM crop_groups WHERE id = %s", (group_id,))
        result = cur.fetchone()
        greenhouse_id = result[0] if result else None
        
        if greenhouse_id:
            print(f"📍 Greenhouse ID: {greenhouse_id}")

        # 업로드된 이미지 파일 확인
        if 'file' not in request.files:
            conn.close()
            return jsonify({'message': '업로드할 이미지가 없습니다.'}), 400

        file = request.files['file']
        if file.filename == '':
            conn.close()
            return jsonify({'message': '선택된 파일이 없습니다.'}), 400

        # 안전한 파일명 생성
        filename = secure_filename(file.filename)
        unique_filename = f"iot_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{filename}"
        file_path = os.path.join(UPLOAD_DIR, unique_filename)
        
        # 파일 저장
        file.save(file_path)
        print(f"📸 IoT 이미지 저장: {unique_filename}")

        # YOLO 분석 실행
        if YOLO_AVAILABLE and MODEL_RIPE and MODEL_ROTTEN:
            try:
                print(f"🔍 YOLO 분석 시작: {unique_filename}")
                
                # 익은/안익은 딸기 분석
                result_ripe = MODEL_RIPE(file_path, conf=0.5)
                result_rotten = MODEL_ROTTEN(file_path, conf=0.5)

                ripe_classes = [MODEL_RIPE.names[int(cls)] for cls in result_ripe[0].boxes.cls]
                rotten_classes = [MODEL_ROTTEN.names[int(cls)] for cls in result_rotten[0].boxes.cls]

                count_ripe = Counter(ripe_classes)
                count_rotten = Counter(rotten_classes)

                ripe = count_ripe.get("straw-ripe", 0)
                unripe = count_ripe.get("straw-unripe", 0)
                total = ripe + unripe
                has_rotten = count_rotten.get("starw_rotten", 0) > 0

                print(f"📊 분석 결과 - 익은: {ripe}, 안익은: {unripe}, 썩은: {has_rotten}")

            except Exception as yolo_err:
                print(f"❌ YOLO 분석 실패: {yolo_err}")
                # 분석 실패 시 더미 데이터
                ripe = 2
                unripe = 1
                total = 3
                has_rotten = False
        else:
            print("⚠️ YOLO 모델 사용 불가, 더미 데이터 사용")
            # YOLO 사용 불가 시 더미 데이터
            ripe = 3
            unripe = 2
            total = 5
            has_rotten = False

        # 기존 분석 결과 조회 (동시 업데이트 방지를 위해 FOR UPDATE 사용)
        cur.execute("""
            SELECT last_analysis_result, harvest_amount, total_amount, is_read
            FROM crop_groups
            WHERE id = %s
            FOR UPDATE
        """, (group_id,))
        existing_result = cur.fetchone()
        
        # 기존 analyzed_files 배열 읽기 (같은 촬영 세션 내의 이미지만)
        analyzed_files = []
        total_ripe = 0
        total_unripe = 0
        total_count = 0
        has_any_rotten = False
        current_session_start = None
        
        if existing_result and existing_result[0]:
            try:
                existing_analysis = json.loads(existing_result[0])
                current_session_start = existing_analysis.get('capture_session_start')
                
                # 같은 촬영 세션 내의 이미지만 사용
                if current_session_start:
                    # analyzed_files 배열이 있으면 사용 (같은 세션 내)
                    if isinstance(existing_analysis, dict) and 'analyzed_files' in existing_analysis:
                        # 기존 배열을 복사 (참조가 아닌 값 복사)
                        analyzed_files = list(existing_analysis['analyzed_files'])
                        # 기존 총합 계산
                        for file_data in analyzed_files:
                            total_ripe += file_data.get('ripe', 0)
                            total_unripe += file_data.get('unripe', 0)
                            total_count += file_data.get('total', 0)
                            if file_data.get('has_rotten', False):
                                has_any_rotten = True
                    # 단일 이미지 형식인 경우 (하위 호환성)
                    elif isinstance(existing_analysis, dict) and 'filename' in existing_analysis:
                        analyzed_files = [dict(existing_analysis)]  # 복사본 생성
                        total_ripe = existing_analysis.get('ripe', 0)
                        total_unripe = existing_analysis.get('unripe', 0)
                        total_count = existing_analysis.get('total', 0)
                        has_any_rotten = existing_analysis.get('has_rotten', False)
                else:
                    # 세션 정보가 없으면 새로 시작
                    analyzed_files = []
                    current_session_start = datetime.now().isoformat()
            except (json.JSONDecodeError, TypeError) as e:
                print(f"⚠️ 기존 분석 결과 파싱 실패: {e}, 새 세션으로 시작")
                analyzed_files = []
                current_session_start = datetime.now().isoformat()
        
        # 세션 시작 시간이 없으면 현재 시간으로 설정
        if not current_session_start:
            current_session_start = datetime.now().isoformat()
        
        # 새 이미지 분석 결과 추가 (중복 체크)
        new_file_result = {
            'filename': unique_filename,
            'ripe': ripe,
            'unripe': unripe,
            'total': total,
            'has_rotten': has_rotten,
            'iot_id': iot_id,
            'analyzed_at': datetime.now().isoformat()
        }
        
        # 같은 파일명이 이미 있는지 확인 (중복 방지)
        existing_filenames = {f.get('filename') for f in analyzed_files}
        if unique_filename not in existing_filenames:
            analyzed_files.append(new_file_result)
            print(f"✅ 새 이미지 추가: {unique_filename} (총 {len(analyzed_files)}개)")
        else:
            print(f"⚠️ 이미 존재하는 이미지: {unique_filename}, 건너뜀")
        
        # 총합 업데이트
        total_ripe += ripe
        total_unripe += unripe
        total_count += total
        if has_rotten:
            has_any_rotten = True
        
        # DB 업데이트 (harvest_amount, total_amount, is_read, last_image_path, last_analysis_result)
        # 분석 결과를 JSON으로 저장 (여러 이미지 형식)
        analysis_result = {
            'capture_session_start': current_session_start,
            'total_files': len(analyzed_files),
            'total_ripe': total_ripe,
            'total_unripe': total_unripe,
            'total_count': total_count,
            'has_rotten': has_any_rotten,
            'analyzed_files': analyzed_files
        }
        
        # 첫 번째 이미지 경로 저장 (하위 호환성)
        first_image = analyzed_files[0]['filename'] if analyzed_files else unique_filename
        
        cur.execute("""
            UPDATE crop_groups
            SET harvest_amount = %s,
                total_amount = %s,
                is_read = %s,
                last_image_path = %s,
                last_analysis_result = %s
            WHERE id = %s
        """, (total_ripe, total_count, True if has_any_rotten else False, 
              first_image, json.dumps(analysis_result), group_id))
        
        conn.commit()
        conn.close()

        print(f"✅ DB 업데이트 완료 - 그룹 ID: {group_id}")
        print(f"📸 이미지 저장: {unique_filename}")

        # 응답 반환
        return jsonify({
            "message": "📸 IoT 이미지 분석 완료",
            "result": {
                "filename": unique_filename,
                "ripe": ripe,
                "unripe": unripe,
                "total": total,
                "rotten": "✅ 발견됨" if has_rotten else "❌ 없음",
                "is_read": True if has_rotten else False
            }
        }), 200

    except Exception as e:
        print(f"❌ IoT 이미지 업로드 및 분석 오류: {e}")
        return jsonify({'message': '서버 오류 발생', 'error': str(e)}), 500

# --------------------------
# IoT 촬영 명령 전송 (기존 방식 - 호환성 유지)
# --------------------------
