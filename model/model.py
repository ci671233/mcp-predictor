import pandas as pd
import numpy as np
from prophet import Prophet
from prophet.diagnostics import cross_validation, performance_metrics
from sklearn.metrics import mean_squared_error
from lightgbm import LGBMRegressor
import optuna
from multiprocessing import Pool, cpu_count
from tqdm import tqdm
import warnings
import random
warnings.filterwarnings('ignore')

# 랜덤 시드 설정
random.seed(42)
np.random.seed(42)

# ✅ 멀티프로세싱을 위한 전역 함수로 분리
def process_single_task(args):
    """멀티프로세싱용 전역 함수"""
    region, group_data, target, time_col, future_steps, group_key, config, all_data = args
    processor = ModelProcessor(config)
    return processor.process_region_target(region, group_data, target, time_col, future_steps, group_key, all_data)


class ModelProcessor:
    """모델 처리를 위한 헬퍼 클래스"""
    def __init__(self, config):
        self.config = config

    def preprocess_data(self, df):
        """이상치 제거 및 데이터 정제"""
        df = df.copy()
        for col in df.select_dtypes(include=[np.number]).columns:
            if df[col].std() > 1e-9:  # 표준편차가 0이 아닌 경우만
                z_scores = (df[col] - df[col].mean()) / df[col].std()
                df.loc[z_scores.abs() > 3, col] = np.nan
            df[col] = df[col].interpolate(method='linear').fillna(method='bfill').fillna(method='ffill')
        return df

    def add_features(self, group, target):
        """시계열 피처 엔지니어링"""
        group = group.copy()
        
        # 기본 lag 피처
        group['lag1'] = group[target].shift(1)
        group['lag2'] = group[target].shift(2)
        group['lag3'] = group[target].shift(3)
        
        # 이동평균 피처
        group['rolling_mean_2'] = group[target].rolling(window=2, min_periods=1).mean()
        group['rolling_mean_3'] = group[target].rolling(window=3, min_periods=1).mean()
        group['rolling_mean_5'] = group[target].rolling(window=5, min_periods=1).mean()
        
        # 이동표준편차 피처
        group['rolling_std_3'] = group[target].rolling(window=3, min_periods=1).std()
        group['rolling_std_5'] = group[target].rolling(window=5, min_periods=1).std()
        
        # 변화율 피처
        group['change_rate'] = group[target].pct_change()
        group['change_rate_2'] = group[target].pct_change(periods=2)
        
        # 추세 피처
        group['trend'] = np.arange(len(group))
        group['trend_squared'] = group['trend'] ** 2
        
        # 계절성 피처 (연도 기반)
        group['year'] = group.index.astype(int) if group.index.dtype == 'int64' else np.arange(len(group))
        group['year_sin'] = np.sin(2 * np.pi * group['year'] / 10)  # 10년 주기
        group['year_cos'] = np.cos(2 * np.pi * group['year'] / 10)
        
        # 통계적 피처
        group['z_score'] = (group[target] - group[target].rolling(window=5, min_periods=1).mean()) / \
                          (group[target].rolling(window=5, min_periods=1).std() + 1e-8)
        
        # 결측치 처리
        group = group.fillna(method='bfill').fillna(method='ffill').fillna(0)
        
        # 무한값 처리
        group = group.replace([np.inf, -np.inf], 0)
        
        return group

    def tune_prophet(self, df):
        """Prophet 하이퍼파라미터 튜닝 - 단순화"""
        return {
            "changepoint_prior_scale": 0.05,
            "seasonality_prior_scale": 1.0,
            "seasonality_mode": "additive",
            "changepoint_range": 0.8
        }

    def tune_lgbm(self, X, y):
        """LightGBM 하이퍼파라미터 튜닝 - 단순화"""
        return {
            "num_leaves": 10,
            "max_depth": 5,
            "learning_rate": 0.1,
            "n_estimators": 50,
            "min_child_samples": 1,
            "min_data_in_bin": 1,
            "min_split_gain": 0.01,
            "subsample": 0.8,
            "colsample_bytree": 0.8
        }

    def process_region_target(self, region, group_data, target, time_col, future_steps, group_key, all_data=None):
        """단일 (region, target) 처리 - 전체 데이터 참고 + 정교한 트렌드 판단"""
        try:
            group = group_data.copy()
            group = group.sort_values(by=time_col).reset_index(drop=True)
            
            valid_data = group[target].notna()
            if valid_data.sum() < 1:
                return self._create_empty_result(region, future_steps, group_key, time_col, target)

            group_clean = group[valid_data].copy()
            
            if group_clean[time_col].dtype == 'object':
                group_clean[time_col] = pd.to_numeric(group_clean[time_col], errors='coerce')
            
            group_clean = group_clean.dropna(subset=[time_col])
            
            if len(group_clean) < 1:
                return self._create_empty_result(region, future_steps, group_key, time_col, target)

            # 데이터 품질 검증
            target_values = group_clean[target].astype(float)
            
            # 정교한 트렌드/계절성 분석
            trend_strength = self._trend_strength(target_values)
            seasonality_strength = self._seasonality_strength(target_values)
            
            # 트렌드가 명확하고 계절성이 약하면 단순 선형 트렌드 기반 예측
            if trend_strength > 0.05 and seasonality_strength < 0.3:
                final_preds = self._simple_trend_based_prediction(target_values, future_steps)
            else:
                # Prophet/LightGBM 보조적 사용
                final_preds = self._prophet_lgbm_prediction(group_clean, target, time_col, future_steps)
            
            # 미래 연도 계산
            last_year = int(group_clean[time_col].max())
            future_years = list(range(last_year + 1, last_year + future_steps + 1))
            
            # 결과 DataFrame 생성
            result_df = pd.DataFrame({
                group_key: [region] * future_steps,
                time_col: future_years,
                target: final_preds
            })
            result_df['_target_name'] = target
            return result_df

        except Exception as e:
            print(f"[WARNING] {region}-{target} 처리 중 오류: {str(e)}")
            return self._create_empty_result(region, future_steps, group_key, time_col, target)

    def _simple_trend_based_prediction(self, historical_values, future_steps):
        """단순하고 안정적인 트렌드 기반 예측 - 정확도 향상"""
        if len(historical_values) < 1:
            return [historical_values.iloc[-1]] * future_steps
        
        # 마지막 값
        last_value = historical_values.iloc[-1]
        
        # 전체 데이터로 트렌드 분석 (제한 없이)
        x = np.arange(len(historical_values))
        y = historical_values
        slope = np.polyfit(x, y, 1)[0]
        
        # R-squared 계산으로 트렌드 신뢰도 측정
        y_pred = slope * x + np.polyfit(x, y, 1)[1]
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0
        
        # 트렌드 신뢰도가 낮으면 보수적으로 조정
        if r_squared < 0.3:
            slope = slope * 0.5  # 트렌드를 절반으로 줄임
        
        # 예측값 생성 - 더 보수적이고 자연스럽게
        predictions = []
        current_value = last_value
        
        for i in range(1, future_steps+1):
            # 기본 트렌드 적용
            pred = last_value + slope * i
            
            # 연속성 강화: 전년도 대비 5% 이상 차이 나면 보정 (더 엄격하게)
            if predictions:
                max_change = predictions[-1] * 0.05  # 최대 5% 변화
                if abs(pred - predictions[-1]) > max_change:
                    if pred > predictions[-1]:
                        pred = predictions[-1] + max_change
                    else:
                        pred = predictions[-1] - max_change
            
            # 음수 방지
            pred = max(0, pred)
            predictions.append(pred)
        
        return predictions

    def _get_global_trend_slope(self, all_data, target):
        """전체 데이터에서 유사한 패턴의 기울기 추정"""
        if len(all_data) == 0:
            return 0
        
        # 모든 지역의 해당 target 데이터 수집
        all_target_values = []
        for region_data in all_data.values():
            if target in region_data.columns:
                values = region_data[target].dropna()
                if len(values) > 0:
                    all_target_values.append(values)
        
        if len(all_target_values) == 0:
            return 0
        
        # 전체 기울기들의 평균 계산
        slopes = []
        for values in all_target_values:
            if len(values) >= 2:
                x = np.arange(len(values))
                slope = np.polyfit(x, values, 1)[0]
                slopes.append(slope)
        
        if len(slopes) == 0:
            return 0
        
        return np.mean(slopes)

    def _trend_strength(self, y):
        """트렌드 강도 계산 - 더 정교한 판단"""
        if len(y) < 2:
            return 0
        
        # 선형 회귀로 기울기 계산
        x = np.arange(len(y))
        slope = np.polyfit(x, y, 1)[0]
        
        # R-squared 계산으로 트렌드의 설명력 측정
        y_pred = slope * x + np.polyfit(x, y, 1)[1]
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0
        
        # 트렌드 강도 = 기울기의 절대값 * R-squared * 데이터 길이 가중치
        data_length_weight = min(len(y) / 10.0, 1.0)  # 데이터가 많을수록 가중치 높음
        trend_strength = abs(slope) * r_squared * data_length_weight / (np.std(y) + 1e-8)
        
        return trend_strength

    def _seasonality_strength(self, y):
        """계절성 강도 계산 - 더 정교한 판단"""
        if len(y) < 4:
            return 0
        
        # 변화량의 표준편차로 계절성 측정
        diff = np.diff(y)
        seasonality_strength = np.std(diff) / (np.std(y) + 1e-8)
        
        # 데이터 길이에 따른 가중치 적용
        data_length_weight = min(len(y) / 10.0, 1.0)
        seasonality_strength = seasonality_strength * data_length_weight
        
        return seasonality_strength

    def _prophet_lgbm_prediction(self, group_clean, target, time_col, future_steps):
        """Prophet/LightGBM 보조적 예측 (트렌드가 불명확하거나 계절성이 강할 때만)"""
        # Prophet 데이터 준비
        prophet_df = pd.DataFrame({
            'ds': pd.to_datetime(group_clean[time_col].astype(int).astype(str) + '-01-01'),
            'y': group_clean[target].astype(float)
        })
        best_prophet_params = self.tune_prophet(prophet_df)
        prophet_model = Prophet(
            changepoint_prior_scale=best_prophet_params.get('changepoint_prior_scale', 0.05),
            seasonality_prior_scale=best_prophet_params.get('seasonality_prior_scale', 1.0),
            changepoint_range=best_prophet_params.get('changepoint_range', 0.8),
            yearly_seasonality=True,
            weekly_seasonality=False,
            daily_seasonality=False,
            seasonality_mode=best_prophet_params.get('seasonality_mode', 'additive')
        )
        prophet_model.fit(prophet_df)
        last_year = int(group_clean[time_col].max())
        future_years = list(range(last_year + 1, last_year + future_steps + 1))
        future_dates = pd.DataFrame({'ds': pd.to_datetime([f"{yr}-01-01" for yr in future_years])})
        prophet_preds = prophet_model.predict(future_dates)['yhat'].values
        return prophet_preds

    def _predict_lgb_sequential(self, model, group_data, features, target, future_steps, future_years):
        """LightGBM 순차 예측 - 제거 예정"""
        # 단순한 트렌드 기반 예측으로 대체되었으므로 이 함수는 사용하지 않음
        return np.array([group_data[target].iloc[-1]] * future_steps)

    def _apply_strong_trend(self, predictions, historical_values, future_steps):
        """예측값에 강력한 트렌드를 적용 - 제거 예정"""
        # 단순한 트렌드 기반 예측으로 대체되었으므로 이 함수는 사용하지 않음
        return predictions

    def _calculate_model_error(self, model, *args):
        """모델 오차 계산"""
        try:
            if hasattr(model, 'predict') and len(args) == 2:
                X, y = args
                preds = model.predict(X)
                return np.sqrt(mean_squared_error(y, preds))
            else:
                df = args[0]
                preds = model.predict(df)['yhat']
                return np.sqrt(mean_squared_error(df['y'], preds))
        except Exception:
            return 1.0

    def _create_empty_result(self, region, future_steps, group_key, time_col, target):
        """빈 결과 DataFrame 생성 - 완전히 동적"""
        result_df = pd.DataFrame({
            group_key: [region] * future_steps,    # 사용자 설정 컬럼명 사용
            time_col: [np.nan] * future_steps,     # 사용자 설정 컬럼명 사용
            target: [np.nan] * future_steps
        })
        result_df['_target_name'] = target
        return result_df

    def _detect_trend(self, series):
        """시계열에서 트렌드 감지 - 단순화"""
        if len(series) < 3:
            return False
        
        # 선형 회귀로 트렌드 검사
        x = np.arange(len(series))
        slope = np.polyfit(x, series, 1)[0]
        return abs(slope) > series.std() * 0.05

    def _detect_seasonality(self, series):
        """시계열에서 계절성 감지 - 단순화"""
        if len(series) < 6:
            return False
        
        # 간단한 계절성 검사
        diff = np.diff(series)
        return np.std(diff) > series.std() * 0.1

    def _validate_and_adjust_predictions(self, predictions, historical_values, future_steps):
        """예측값 검증 및 조정 - 단순화"""
        if len(predictions) == 0:
            return predictions
        
        # 기본적인 검증만 수행
        predictions = np.maximum(predictions, 0)
        
        # 극단적인 값 조정
        mean_val = np.mean(historical_values) if len(historical_values) > 0 else 0
        std_val = np.std(historical_values) if len(historical_values) > 0 else 1
        
        for i in range(len(predictions)):
            if abs(predictions[i] - mean_val) > 3 * std_val:
                predictions[i] = mean_val + np.random.normal(0, std_val * 0.5)
        
        return predictions

class Model:
    def __init__(self, config):
        self.config = config
        self.models = {}
        # ✅ 원본 데이터 타입 저장
        self.original_dtypes = {}

    def train_and_predict(self, X, y):
        """메인 학습 및 예측 함수 - 원본 데이터 타입 보존"""
        time_col = self.config["prediction"]["time_col"]
        group_key = self.config["prediction"]["group_key"]
        future_steps = self.config["prediction"]["future_steps"]

        print(f"[INFO] 시계열 예측 시작 - Time Col: {time_col}, Group: {group_key}")
        
        # ✅ 1. 원본 데이터 타입 저장
        df = X.copy()
        for col in y.columns:
            df[col] = y[col]
        
        # 모든 컬럼의 원본 타입 저장
        self.original_dtypes = {col: df[col].dtype for col in df.columns}
        print(f"[INFO] 원본 데이터 타입 저장:")
        for col, dtype in self.original_dtypes.items():
            print(f"  {col}: {dtype}")
        
        # ✅ 2. 내부 처리용 타입 변환 (시간 컬럼만)
        original_time_dtype = df[time_col].dtype
        df[time_col] = pd.to_numeric(df[time_col], errors='coerce')
        df = df.dropna(subset=[time_col])
        
        if len(df) == 0:
            raise ValueError(f"시간 컬럼 '{time_col}'을 숫자형으로 변환할 수 없습니다.")

        # 태스크 생성
        tasks = []
        for region, group_data in df.groupby(group_key):
            for target in y.columns:
                tasks.append((region, group_data, target, time_col, future_steps, group_key, self.config, df)) # all_data 추가

        print(f"[INFO] 총 {len(tasks)}개 태스크 생성")

        # 멀티프로세싱 실행
        results = []
        n_processes = max(1, min(cpu_count() - 1, len(tasks)))
        
        with Pool(processes=n_processes) as pool:
            for result in tqdm(pool.imap_unordered(process_single_task, tasks),
                             total=len(tasks), desc="예측 진행", unit="task"):
                results.append(result)

        if not results:
            print("[WARNING] 예측 결과가 없습니다.")
            return pd.DataFrame()

        # Wide Format으로 변환
        print("[INFO] 결과를 Wide Format으로 변환 중...")
        wide_format_df = self._convert_to_wide_format(results, group_key, time_col, y.columns)
        
        # ✅ 3. 원본 데이터 타입으로 복원
        wide_format_df = self._restore_original_dtypes(wide_format_df)
            
        print(f"[INFO] 예측 완료 - 총 {len(wide_format_df)} rows 생성")
        return wide_format_df

    def _restore_original_dtypes(self, df):
        """원본 데이터 타입으로 복원 - 동적 컬럼 처리"""
        print("[INFO] 원본 데이터 타입으로 복원 중...")
        
        df_restored = df.copy()
        time_col = self.config["prediction"]["time_col"]  # 사용자 설정값
        
        for col in df_restored.columns:
            if col in self.original_dtypes:
                original_dtype = self.original_dtypes[col]
                current_dtype = df_restored[col].dtype
                
                try:
                    if pd.api.types.is_integer_dtype(original_dtype):
                        df_restored[col] = df_restored[col].fillna(0).round().astype('Int64')
                        print(f"  {col}: {current_dtype} → Int64 (원본: {original_dtype})")
                        
                    elif pd.api.types.is_float_dtype(original_dtype):
                        df_restored[col] = df_restored[col].astype('float64')
                        print(f"  {col}: {current_dtype} → float64 (원본: {original_dtype})")
                        
                    elif pd.api.types.is_object_dtype(original_dtype):
                        if col == time_col:  # ✅ 동적 시간 컬럼 확인
                            df_restored[col] = df_restored[col].astype(str)
                        else:
                            df_restored[col] = df_restored[col].astype(str)
                        print(f"  {col}: {current_dtype} → object (원본: {original_dtype})")
                        
                    elif 'int' in str(original_dtype).lower():
                        df_restored[col] = df_restored[col].fillna(0).round().astype('Int64')
                        print(f"  {col}: {current_dtype} → Int64 (원본: {original_dtype})")
                        
                    else:
                        print(f"  {col}: {current_dtype} → 변환 안함 (원본: {original_dtype})")
                        
                except Exception as e:
                    print(f"  [WARNING] {col} 타입 복원 실패: {e}")
                    # ✅ 안전한 기본값 처리 (target 컬럼 패턴으로 판단)
                    if 'STDNT_NOPE' in col or any(target_pattern in col for target_pattern in ['_CNT', '_NUM', '_COUNT']):
                        df_restored[col] = df_restored[col].fillna(0).round().astype('Int64')
                    else:
                        df_restored[col] = df_restored[col].astype(str)
        
        return df_restored

    def _convert_to_wide_format(self, results, group_key, time_col, target_columns):
        """개별 target 결과를 Wide Format으로 병합 - 중복 컬럼 문제 해결"""
        
        # 유효한 결과만 필터링
        valid_results = []
        for result_df in results:
            if '_target_name' in result_df.columns and len(result_df) > 0:
                target_name = result_df['_target_name'].iloc[0]
                if not result_df[target_name].isna().all():
                    valid_results.append(result_df)

        if not valid_results:
            print("[WARNING] 유효한 예측 결과가 없습니다.")
            return pd.DataFrame()

        print(f"[INFO] 유효한 결과 수: {len(valid_results)}")

        # 모든 지역-년도 조합 생성
        all_regions = set()
        all_years = set()
        
        for result_df in valid_results:
            all_regions.update(result_df[group_key].unique())
            all_years.update(result_df[time_col].dropna().unique())
        
        # 기준 프레임 생성
        from itertools import product
        base_combinations = list(product(sorted(all_regions), sorted(all_years)))
        base_df = pd.DataFrame(base_combinations, columns=[group_key, time_col])
        
        print(f"[INFO] 기준 프레임 생성: {len(base_df)} rows ({len(all_regions)} regions × {len(all_years)} years)")

        # ✅ Target별로 그룹화하여 중복 제거
        target_results = {}
        for result_df in valid_results:
            target_name = result_df['_target_name'].iloc[0]
            
            if target_name not in target_results:
                target_results[target_name] = []
            
            # 해당 target의 데이터만 추출
            merge_df = result_df.drop(columns=['_target_name']).copy()
            merge_df = merge_df.dropna(subset=[group_key, time_col])
            target_results[target_name].append(merge_df)

        # ✅ 각 target별로 먼저 통합한 후 병합
        for target_name in target_results:
            print(f"[INFO] {target_name} 처리 중... ({len(target_results[target_name])} 개 결과)")
            
            if len(target_results[target_name]) == 1:
                # 단일 결과인 경우
                target_df = target_results[target_name][0][[group_key, time_col, target_name]]
            else:
                # 여러 결과를 통합
                combined_data = []
                for df in target_results[target_name]:
                    if target_name in df.columns:
                        combined_data.append(df[[group_key, time_col, target_name]])
                
                if combined_data:
                    # 중복 제거하며 통합 (같은 지역-년도는 평균값 사용)
                    target_df = pd.concat(combined_data, ignore_index=True)
                    target_df = target_df.groupby([group_key, time_col], as_index=False)[target_name].mean()
                else:
                    continue
            
            # 기준 프레임에 병합
            if target_name in base_df.columns:
                # 이미 존재하는 컬럼인 경우 업데이트
                base_df = base_df.drop(columns=[target_name])
            
            base_df = base_df.merge(
                target_df, 
                on=[group_key, time_col], 
                how='left'
            )
            
            print(f"[INFO] {target_name} 병합 완료")

        # ✅ 모든 target 컬럼이 존재하는지 확인하고 추가
        for target in target_columns:
            if target not in base_df.columns:
                base_df[target] = np.nan
                print(f"[WARNING] {target} 컬럼이 없어서 NaN으로 추가")

        # 컬럼 순서 정리
        column_order = [group_key, time_col] + list(target_columns)
        base_df = base_df[column_order]

        # 정렬
        base_df = base_df.sort_values([group_key, time_col]).reset_index(drop=True)
        
        print(f"[INFO] Wide Format 변환 완료: {base_df.shape}")
        print(f"[INFO] 최종 컬럼: {list(base_df.columns)}")
        
        return base_df