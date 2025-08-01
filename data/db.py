import ibm_db
import ibm_db_dbi
import pandas as pd
import numpy as np

class Database:
    def __init__(self, config):
        self.config = config
        self.db_type = config.get("db_type", "").lower()

    def load(self):
        if self.db_type == "db2":
            return self._load_db2()
        else:
            raise ValueError(f"지원하지 않는 DB 타입: {self.db_type}")

    def save(self, df):
        if self.db_type == "db2":
            self._save_db2(df)
        else:
            raise ValueError(f"지원하지 않는 DB 타입: {self.db_type}")

    # ------------------- DB2 -------------------
    def _load_db2(self):
        """DB2에서 데이터 로드"""
        conn_str = (
            f"DATABASE={self.config['connection']['database']};"
            f"HOSTNAME={self.config['connection']['host']};"
            f"PORT={self.config['connection']['port']};"
            f"PROTOCOL=TCPIP;"
            f"UID={self.config['connection']['user']};"
            f"PWD={self.config['connection']['password']};"
        )
        
        target = self.config['target']
        if isinstance(target, str):
            target = [target]
        
        query = f"SELECT {', '.join(self.config['features'] + target)} FROM {self.config['table']}"
        print(f"[INFO] 실행 쿼리: {query}")

        conn = ibm_db.connect(conn_str, "", "")
        try:
            pconn = ibm_db_dbi.Connection(conn)
            df = pd.read_sql(query, pconn)
            print(f"[INFO] DB2에서 {len(df)} rows 로드 완료")
        finally:
            ibm_db.close(conn)

        return df

    def _save_db2(self, df):
        """DB2에 예측 결과 저장 - 완전히 동적 처리"""
        if df is None or len(df) == 0:
            print("[WARNING] 저장할 데이터가 없습니다.")
            return

        print(f"[INFO] 저장 전 데이터 검증 및 전처리 시작...")
        
        # ✅ 1. 데이터 전처리
        df_clean = self._preprocess_for_db(df)
        
        if len(df_clean) == 0:
            print("[WARNING] 전처리 후 유효한 데이터가 없습니다.")
            return

        # ✅ 설정에서 컬럼명 가져오기
        prediction_config = self.config.get('prediction', {})
        group_col = prediction_config.get('group_key')  # 예: "RGN_CD"
        time_col = prediction_config.get('time_col')    # 예: "CRTR_YR"

        conn_str = (
            f"DATABASE={self.config['connection']['database']};"
            f"HOSTNAME={self.config['connection']['host']};"
            f"PORT={self.config['connection']['port']};"
            f"PROTOCOL=TCPIP;"
            f"UID={self.config['connection']['user']};"
            f"PWD={self.config['connection']['password']};"
        )
        
        conn = None
        pconn = None
        cursor = None
        
        try:
            # 연결 생성
            conn = ibm_db.connect(conn_str, "", "")
            if conn is None:
                raise Exception("DB 연결 실패")
            
            pconn = ibm_db_dbi.Connection(conn)
            cursor = pconn.cursor()

            # BSC_SN 최대값 조회
            cursor.execute(f"SELECT COALESCE(MAX(BSC_SN), 0) FROM {self.config['table']}")
            max_sn_result = cursor.fetchone()[0] or 0
            # decimal.Decimal을 int로 변환
            max_sn = int(max_sn_result) if max_sn_result is not None else 0
            print(f"[INFO] 현재 최대 BSC_SN: {max_sn}")

            # 메타데이터 컬럼 추가
            df_to_save = df_clean.copy()
            now = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')
            
            df_to_save["BSC_SN"] = range(max_sn + 1, max_sn + 1 + len(df_to_save))
            df_to_save["FRST_REG_DT"] = now
            df_to_save["FRST_REG_USER_ID"] = "SYSTEM"
            df_to_save["LAST_MDFCN_DT"] = now
            df_to_save["LAST_MDFCN_USER_ID"] = "SYSTEM"
            df_to_save["DEL_YN"] = "N"

            # 컬럼 순서 정리
            meta_cols = ["BSC_SN", "FRST_REG_DT", "FRST_REG_USER_ID", "LAST_MDFCN_DT", "LAST_MDFCN_USER_ID", "DEL_YN"]
            data_cols = [col for col in df_to_save.columns if col not in meta_cols]
            final_cols = ["BSC_SN"] + data_cols + meta_cols[1:]
            
            df_to_save = df_to_save[final_cols]

            print(f"[INFO] 최종 저장 데이터:")
            print(f"  - Shape: {df_to_save.shape}")
            print(f"  - Columns: {list(df_to_save.columns)}")
            print("  - Data types:")
            for col in df_to_save.columns:
                print(f"    {col}: {df_to_save[col].dtype}")
            
            print("  - Sample data:")
            print(df_to_save.head(3))
            print("  - NaN count per column:")
            print(df_to_save.isnull().sum())

            # ✅ 2. 배치별 저장 (완전히 동적 처리)
            batch_size = 1000
            total_saved = 0
            
            placeholders = ', '.join(['?'] * len(final_cols))
            sql = f"INSERT INTO {self.config['table']} ({', '.join(final_cols)}) VALUES ({placeholders})"
            
            for i in range(0, len(df_to_save), batch_size):
                batch_df = df_to_save.iloc[i:i+batch_size]
                
                # ✅ 데이터 타입 최종 검증 및 변환 (동적 처리)
                batch_tuples = []
                for _, row in batch_df.iterrows():
                    row_tuple = []
                    for col in final_cols:
                        val = row[col]
                        if pd.isna(val):
                            row_tuple.append(None)
                        elif col == "BSC_SN":
                            row_tuple.append(int(val))
                        elif col in [group_col, time_col]:  # ✅ 동적 컬럼명 사용
                            row_tuple.append(str(val))
                        elif "STDNT_NOPE" in col:  # target 컬럼들
                            row_tuple.append(float(val))
                        else:  # 메타데이터 컬럼들
                            row_tuple.append(str(val))
                    batch_tuples.append(tuple(row_tuple))
                
                print(f"[INFO] 배치 {i//batch_size + 1}: {len(batch_tuples)}개 레코드 삽입...")
                cursor.executemany(sql, batch_tuples)
                total_saved += len(batch_tuples)
            
            pconn.commit()
            print(f"[SUCCESS] DB2에 총 {total_saved}건 저장 완료!")
            print(f"          BSC_SN 범위: {max_sn+1} ~ {max_sn+total_saved}")

        except Exception as e:
            if pconn:
                try:
                    pconn.rollback()
                except:
                    pass
            print(f"[ERROR] DB2 저장 실패: {str(e)}")
            raise

        finally:
            # 연결 정리
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            if pconn:
                try:
                    pconn.close()
                except:
                    pass
            if conn:
                try:
                    ibm_db.close(conn)
                except:
                    pass

    def _preprocess_for_db(self, df):
        """DB 저장을 위한 데이터 전처리 - 완전히 동적 처리"""
        df_clean = df.copy()
        
        # ✅ 설정에서 실제 컬럼명 가져오기 (하드코딩 제거)
        prediction_config = self.config.get('prediction', {})
        group_col = prediction_config.get('group_key')  # 사용자 설정값
        time_col = prediction_config.get('time_col')    # 사용자 설정값
        
        print(f"[INFO] 사용할 컬럼명 - Group: {group_col}, Time: {time_col}")
        print(f"[INFO] 전처리 전 데이터 상태:")
        print(f"  - Shape: {df_clean.shape}")
        print(f"  - Columns: {list(df_clean.columns)}")
        print(f"  - NaN counts:\n{df_clean.isnull().sum()}")
        
        # ✅ 1. 완전히 빈 행 제거
        before_len = len(df_clean)
        target_cols = [col for col in df_clean.columns if "STDNT_NOPE" in col]
        df_clean = df_clean.dropna(subset=target_cols, how='all')
        print(f"[INFO] 완전 빈 행 제거: {before_len} → {len(df_clean)} rows")
        
        # ✅ 2. 숫자형 컬럼 처리 (target 컬럼들)
        for col in target_cols:
            if col in df_clean.columns:
                df_clean[col] = df_clean[col].fillna(0.0)
                df_clean[col] = df_clean[col].clip(lower=0.0)
                df_clean[col] = df_clean[col].round(2)
        
        # ✅ 3. 동적 컬럼 처리 (컬럼명 하드코딩 제거)
        # 그룹 컬럼 처리 (예: RGN_CD, REGION_ID, AREA_CODE 등 어떤 이름이든)
        if group_col and group_col in df_clean.columns:
            # 문자열로 변환하되, 구체적인 자릿수는 데이터에 따라 결정
            df_clean[group_col] = df_clean[group_col].astype(str)
            print(f"[INFO] {group_col} 컬럼을 문자열로 변환완료")
        
        # 시간 컬럼 처리 (예: CRTR_YR, YEAR, DATE 등 어떤 이름이든)
        if time_col and time_col in df_clean.columns:
            # 기본값을 현재 년도로 설정하고 정수형 변환 후 문자열로
            current_year = pd.Timestamp.now().year
            df_clean[time_col] = df_clean[time_col].fillna(current_year).astype(int).astype(str)
            print(f"[INFO] {time_col} 컬럼을 문자열로 변환 완료")
        
        # ✅ 4. 필수 컬럼 누락 확인 (동적)
        essential_cols = []
        if group_col:
            essential_cols.append(group_col)
        if time_col:
            essential_cols.append(time_col)
        
        for col in essential_cols:
            if col in df_clean.columns:
                before_drop = len(df_clean)
                df_clean = df_clean.dropna(subset=[col])
                after_drop = len(df_clean)
                if before_drop != after_drop:
                    print(f"[INFO] {col} 누락 행 제거: {before_drop} → {after_drop}")
        
        print(f"[INFO] 전처리 후 데이터 상태:")
        print(f"  - Shape: {df_clean.shape}")
        print(f"  - Final columns: {list(df_clean.columns)}")
        
        return df_clean


    # # ------------------- MySQL -------------------
    # def _load_mysql(self):
    #     target = self.config['target']
    #     if isinstance(target, str):
    #         target = [target]
    #     query = f"SELECT {', '.join(self.config['features'] + target)} FROM {self.config['table']}"

    #     conn = pymysql.connect(
    #         host=self.config['connection']['host'],
    #         user=self.config['connection']['user'],
    #         password=self.config['connection']['password'],
    #         database=self.config['connection']['database'],
    #         port=int(self.config['connection']['port']),
    #         charset="utf8mb4"
    #     )
    #     df = pd.read_sql(query, conn)
    #     conn.close()
    #     return df

    # def _save_mysql(self, df):
    #     conn = pymysql.connect(
    #         host=self.config['connection']['host'],
    #         user=self.config['connection']['user'],
    #         password=self.config['connection']['password'],
    #         database=self.config['connection']['database'],
    #         port=int(self.config['connection']['port']),
    #         charset="utf8mb4"
    #     )
    #     cursor = conn.cursor()
    #     cols = list(df.columns)
    #     sql = f"INSERT INTO {self.config['table']} ({', '.join(cols)}) VALUES ({', '.join(['%s']*len(cols))})"
    #     data = [tuple(None if pd.isna(x) else x for x in row) for row in df.to_numpy()]
    #     cursor.executemany(sql, data)
    #     conn.commit()
    #     cursor.close()
    #     conn.close()
    #     print(f"[INFO] MySQL에 {len(df)}건 저장 완료")

    # # ------------------- MongoDB -------------------
    # def _load_mongo(self):
    #     client = MongoClient(
    #         host=self.config['connection']['host'],
    #         port=int(self.config['connection']['port']),
    #         username=self.config['connection']['user'],
    #         password=self.config['connection']['password'],
    #         authSource=self.config['connection']['database']
    #     )
    #     db = client[self.config['connection']['database']]
    #     target = self.config['target']
    #     if isinstance(target, str):
    #         target = [target]
    #     projection = {col: 1 for col in self.config['features'] + target}
    #     data = list(db[self.config['table']].find({}, projection))
    #     client.close()
    #     return pd.DataFrame(data)

    # def _save_mongo(self, df):
    #     client = MongoClient(
    #         host=self.config['connection']['host'],
    #         port=int(self.config['connection']['port']),
    #         username=self.config['connection']['user'],
    #         password=self.config['connection']['password'],
    #         authSource=self.config['connection']['database']
    #     )
    #     db = client[self.config['connection']['database']]
    #     data = df.to_dict(orient="records")
    #     if data:
    #         db[self.config['table']].insert_many(data)
    #         print(f"[INFO] MongoDB에 {len(data)}건 저장 완료")
    #     client.close()
