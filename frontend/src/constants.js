export const ROLES={admin:'运维主管',planner:'清洗计划员',viewer:'电站观察员'};
export const SURVEY_STATES={recorded:'待评估',evaluating:'评估中',assessed:'已评估',scheduled:'清洗中',retested:'已复测',closed:'已闭环',blocked:'需重试',stale:'清洗前旧读数'};
export const MISSION_STATES={draft:'待派工',dispatched:'执行中',retested:'待验收',accepted:'已验收',superseded:'旧计划已失效'};
export const EXECUTION_STATES={active:'作业中',paused:'暂停待接单'};
export const PERMIT_STATES={draft:'草稿',signed:'已签发',used:'已派工',paused:'暂停保留',pending:'待接单',void:'已撤销/作废'};
export const PERMIT_KINDS={initial:'初始许可',continuation:'跨日续作'};
