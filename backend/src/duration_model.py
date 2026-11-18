"""用已完工（accepted）清洗任务的分段实耗训练总工时模型，独立留出集报误差。

样本不足（训练集 < MIN_TRAIN_SAMPLES）不出预测，转人工核定，绝不拿训练误差冒充泛化误差。
特征只使用派工时已知的任务/阵列口径，不使用实耗本身。
"""
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error

FEATURES = ['capacity_kw', 'area_m2', 'slope', 'water_liters', 'dust']
MIN_TRAIN_SAMPLES = 8
MIN_COMPLETED_SAMPLES = 12
HOLDOUT_SIZE = 0.25
RANDOM_STATE = 42


def build_training_rows(db, WorkSegment, WashMission, SoilingSurvey, ArrayBlock):
    """聚合已完工任务的分段实耗作为总工时标签；无分段实耗的历史任务不参与。"""
    from sqlalchemy import select, func
    rows = db.execute(
        select(
            SoilingSurvey.capacity_kw, ArrayBlock.area_m2, ArrayBlock.slope,
            WashMission.water_liters, SoilingSurvey.dust,
            func.coalesce(func.sum(WorkSegment.actual_minutes), 0),
        )
        .select_from(WashMission)
        .join(SoilingSurvey, SoilingSurvey.id == WashMission.survey_id)
        .join(ArrayBlock, ArrayBlock.id == SoilingSurvey.block_id)
        .join(WorkSegment, WorkSegment.mission_id == WashMission.id)
        .where(WashMission.status == 'accepted')
        .group_by(WashMission.id, SoilingSurvey.capacity_kw, ArrayBlock.area_m2,
                  ArrayBlock.slope, WashMission.water_liters, SoilingSurvey.dust)
    ).all()
    return [
        {'x': [float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])],
         'total_minutes': int(r[5])}
        for r in rows if int(r[5]) > 0
    ]


def train(rows):
    """返回 (model, evidence)；样本不足返回 (None, evidence)，调用方转人工核定。"""
    evidence = {
        'method': 'RandomForestRegressor / 100 trees / max_depth 8 / random_state 42',
        'features': FEATURES,
        'completed_rows': len(rows),
        'min_completed_samples': MIN_COMPLETED_SAMPLES,
        'min_train_samples': MIN_TRAIN_SAMPLES,
        'manual_review': True,
    }
    if len(rows) < MIN_COMPLETED_SAMPLES:
        evidence['reason'] = (
            f'已完工分段实耗样本 {len(rows)} 条，少于最低样本 {MIN_COMPLETED_SAMPLES} 条，转人工核定')
        return None, evidence
    import numpy as np
    x = np.array([r['x'] for r in rows])
    y = np.array([r['total_minutes'] for r in rows])
    train_x, test_x, train_y, test_y = train_test_split(
        x, y, test_size=HOLDOUT_SIZE, random_state=RANDOM_STATE)
    if len(train_x) < MIN_TRAIN_SAMPLES:
        evidence['reason'] = (
            f'划分后训练样本 {len(train_x)} 条，少于 {MIN_TRAIN_SAMPLES} 条，转人工核定')
        return None, evidence
    model = RandomForestRegressor(
        n_estimators=100, max_depth=8, min_samples_leaf=2,
        random_state=RANDOM_STATE, n_jobs=1)
    model.fit(train_x, train_y)
    evidence.update({
        'manual_review': False,
        'train_rows': len(train_x),
        'holdout_rows': len(test_x),
        'train_mae_minutes': float(mean_absolute_error(train_y, model.predict(train_x))),
        'holdout_mae_minutes': float(mean_absolute_error(test_y, model.predict(test_x))),
        'holdout_mae_pct': float(
            mean_absolute_error(test_y, model.predict(test_x)) / float(test_y.mean()) * 100),
        'feature_importance': dict(zip(FEATURES, model.feature_importances_.tolist())),
    })
    return model, evidence


def predict(db, mission, models):
    """预测任务总工时；consumed_minutes 已知时返回剩余工时。"""
    WorkSegment, WashMission, SoilingSurvey, ArrayBlock = models
    rows = build_training_rows(db, WorkSegment, WashMission, SoilingSurvey, ArrayBlock)
    model, evidence = train(rows)
    survey = db.get(SoilingSurvey, mission.survey_id)
    block = db.get(ArrayBlock, survey.block_id)
    vector = [[float(block.capacity_kw), float(block.area_m2), float(block.slope),
               float(mission.water_liters), float(survey.dust)]]
    from sqlalchemy import select, func
    consumed = int(db.scalar(
        select(func.coalesce(func.sum(WorkSegment.actual_minutes), 0))
        .where(WorkSegment.mission_id == mission.id)))
    used_water = float(db.scalar(
        select(func.coalesce(func.sum(WorkSegment.water_used_liters), 0))
        .where(WorkSegment.mission_id == mission.id))) or 0.0
    if model is None:
        evidence.update({'consumed_minutes': consumed, 'predicted_total_minutes': None,
                         'predicted_remaining_minutes': None,
                         'consumed_water_liters': used_water})
        return None, evidence
    total = max(int(round(float(model.predict(vector)[0]))), consumed)
    evidence.update({
        'consumed_minutes': consumed,
        'predicted_total_minutes': total,
        'predicted_remaining_minutes': max(total - consumed, 0),
        'consumed_water_liters': round(used_water, 3),
    })
    return total, evidence
