# 晴岭运维 · 光伏阵列积灰清洗排程

面向晴岭运维12人山地电站班组：巡测读数估损、清洗水量计划、派工扣额与清洗后功率复测闭环。

现有做法是在纸质巡检单抄逆变器功率、Excel比较辐照和经验清洗；低辐照误判积灰、计划忽视水车余量、清洗后没有验证增益。上游是现场巡检单 `qingling_survey_20261117.csv` 的读数，本应用承接估损与清洗环节，下游电站值长查看实际功率收益与用水余量。

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build --wait
# 启动SEED_DEMO=true自动执行；可重复人工运行
 docker compose exec -T backend python -m scripts.seed_demo
# 收尾，删除本项目演示卷
 docker compose down -v --remove-orphans
```

访问 [前端18223](http://localhost:18223)、[后端API19223/docs](http://localhost:19223/docs)；MySQL映射17723。

|角色 / 账号|权限|典型动作|
|---|---|---|
|admin / admin@qingling.test|全部；仅主管维护阵列、派工与验收|审核水量后派工，复测收益为正时验收|
|planner / planner@qingling.test|巡测、估损、草稿用水计划、复测；无最终派工/验收权限|登记读数，修订超量计划|
|viewer / viewer@qingling.test|六业务页与审计只读|值长查当天损失和实际收益|

演示密码均为 `Solar2026!`；JWT有效8小时。默认数据库密码、签名密钥为本地演示配置。

## 业务模块与状态

1. **阵列与水量**：ArrayBlock持久化编号、分区、装机、坡度、面积及当前可用水量；分区图是示意图，不是测绘地图。清洗执行中禁止修改阵列规格或配额。
2. **巡测与估损**：SoilingSurvey保存辐照、组件温度、无雨日、积灰目测、功率及装机/坡度快照。低于350W/m²禁止评估；评估后不覆盖原输入，修正需新巡测。
3. **损失优先序**：比较清洁参考功率与实测残差，按损失编制WashMission水量计划。建议水量=面积×0.6×(1+0.1×目测积灰)，单位L。草稿可超量保存作修订依据，但不能派工。
4. **派工与复测**：主管派工时锁定任务、巡测、阵列行并核验预算与实际余水，扣减水量；mission=dispatched、survey=scheduled、array=washing。派工后水量不可修改。复测保存实际功率与当时辐照，以同辐照归一化计算增益；主管验收正增益后mission=accepted、survey=closed、array=ready并写last_cleaned。同阵列清洗前旧巡测变stale，其草稿任务变superseded，防止用旧积灰读数再次派工。

不另建复杂车队实体：`ArrayBlock.water_liters`表示该阵列已分配的水车可用清洗配额，不跨分区共用总罐。补水必须建立补给计划、现场确认实收，由主管在阵列不处于清洗中时入额，审计留痕；既有配额不可在阵列详情直接覆盖。

## 真实随机森林与口径

`backend/fixtures/clean_reference.json`含200条固定seed42模拟清洁状态参考样本。特征为装机capacity_kw、辐照irradiance、组件温度module_temp、坡度slope；target为clean_kw。模拟参考遵循容量×辐照/1000×温度效率×坡度效率，加0.65kW标准差噪声，数据生成条件写在下方。

160训练 / 40留出，train_test_split random_state42；RandomForestRegressor 100树、max_depth8、min_samples_leaf2、random_state42。训练MAE和独立留出MAE分开返回，不能把训练误差当泛化指标。模型不使用实测observed_kw作特征；积灰损失=max(0,预测clean_kw−observed_kw)。目测积灰和无雨日用于业务参考，积灰目测参与建议水量，不被伪装成模型特征。

标定适用范围：容量80–150kW、辐照350–1050W/m²、组件温度18–65°C、坡度10–35°。超范围拒评而转人工复核。输入量与模型证据SHA256、训练/留出样本数、特征重要性写入survey.model_evidence，worker同时向共享artifacts卷写JSON。Redis队列由Python worker消费，前端只展示结果。

复测归一化功率=`post_kw * survey.irradiance / post_irradiance`；实测增益=归一化功率−清洗前observed_kw。复测辐照至少350W/m²，功率不得超过额定120%；收益不大于0时拒绝验收，可排查后补测。

参考数据可复现：Python random.Random(42)，循环i=0…199，capacity=[80,100,120,150][i%4]，irradiance uniform(350,1050)，temperature uniform(18,65)，slope uniform(10,35)，clean=capacity×irradiance/1000×(1−.004×(temp−25))×(.98−.0003×(slope−23)^2)+gauss(0,.65)。输入保留两位小数、输出四位，参考文件SHA写入每次估损证据。

## 一天的现场剧本

1. 08:30 计划员登录，查SL-A01南坡一号150kW，分配可用清洗水1500L。
2. 09:00 核对 `qingling_survey_20261117.csv`；SV-1117-A01读数800W/m²、35°C、18天无雨、积灰3.2、实测88kW。
3. 09:30 评估清洁功率和积灰损失，检查独立留出误差。
4. 10:00 另一巡测SV-CLOUD-01为240W/m²，评估被阻止，保留原始记录等待补测。
5. 10:30 编制1600L超量计划，主管派工被阻止，阵列仍1500L。
6. 11:00 修订500L后主管派工，阵列余1000L，巡测和任务同步进入清洗中。
7. 14:00 现场复测110kW、800W/m²，归一化增益22kW；未复测不得验收。
8. 16:00 主管验收，阵列恢复可巡检，旧读数失效；观察员查闭环和水量账。

Seed按email、阵列code、巡测code自然键仅插入缺项，不覆盖用户改动；4阵列、4巡测、3账号，另含一条历史已验收清洗、一条第2天复访和一条待收补给单；200条参考样本作为只读模型fixture。

## 工程、目录与开发

Python3.12 / FastAPI / SQLAlchemy / PyMySQL / MySQL8 / Redis7 / scikit-learn1.6 / Nginx1.27。前端原生HTML/CSS/JS ES modules，无框架。

- backend/src/models.py：三领域实体及审计/账号；schemas.py输入验证；array_routes、survey_routes、mission_routes为领域接口。
- field_service.py：阵列规格/巡测规则、估损入队；wash_service.py：用水计划/事务派工/复测/验收联动；power_model.py：参考训练与评估；worker.py：估损消费与心跳；scripts/seed_demo.py幂等seed。
- frontend/src/pages/：阵列图、巡测簿、损失与水量、派工复测及补给/复访六页；components/field.js复用阵列命名、巡测依据、水量账与状态。api.js、store.js、utils.js分离请求、领域状态、格式/时区/错误反馈。
- 角色、巡测状态、任务状态枚举分别在backend/src/constants.py、frontend/src/constants.js，角色值与账号一致。
- app.py的trace与recover中间件负责耗时与全局可恢复错误；审计记录真实变更账号/动作/时间。UI显示Asia/Shanghai。

本地开发：创建Python3.12 venv，安装backend/requirements.txt，设置DATABASE_URL=`mysql+pymysql://solar:solar@127.0.0.1:17723/solar`、REDIS_URL指向本地Redis；backend目录运行uvicorn src.app:app --reload及python -m src.worker。worker默认/artifacts需要写权限，推荐Compose运行；前端经Nginx反向代理/api/，修改后compose build frontend。

环境变量：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、JWT_SECRET、SEED_DEMO、NETWORK_SUBNET。显式默认子网10.221.24.0/24规避本机默认地址池耗尽，可自行配置。五服务真实健康：MySQL用TCP业务账号SELECT1（避免临时初始化server假健康），backend查询DB/Redis，worker心跳，frontend使用127.0.0.1。命名卷db_data/redis_data/artifacts。首次启动metadata建表，无后续在线迁移工具；测试和截图在源码外test-artifacts/gb-1024。

## License

MIT，见LICENSE。


## 水车补给与洗后复访扩展

**水车补给入额**保存 `WaterDelivery`：阵列外键、唯一单号、计划升数、水源车号、实收量、收货人和入额人。状态为 `planned→received→credited`；实收不得超计划，现场确认后不能改写。只有主管入额，事务锁定单据与阵列并加水一次；清洗中的阵列拒入额，完成复测验收后可重试。已有阵列规格可维护，但水量变更通过补给台账。

**洗后衰减复访**保存 `FollowupSurvey`：已验收清洗任务外键、验收后天数、光照/功率、基线快照、归一化功率、衰减百分比、重巡外键及处置依据。仅已验收任务可回访，同一任务天数递增；光照至少350W/m²。本次功率×验收辐照÷本次辐照，得到验收光照下的功率；衰减%=100×(验收功率−归一化功率)÷验收功率。超过5%标记`reinspect`，否则`stable`。主管以至少5字处置依据关闭；超过5%必须关联验收后新增、同阵列且有效的巡测。不能把旧巡测重新派工，不能更改清洗验收结果，已关闭复访不重复关闭。

接口：`GET/POST /deliveries`、`POST /deliveries/{id}/receive`、`POST /deliveries/{id}/credit`、`GET/POST /followups`、`POST /followups/{id}/close`。计划员可计划送水、确认实收、登记复访；主管负责入额与关闭；观察员只读。业务代码为 `revisit_models.py/revisit_schemas.py/revisit_routes.py/water_service.py/followup_service.py`，两个新页面为 `pages/deliveries.js`、`pages/followups.js`。

扩展seed只补缺项：WD-DEMO-01西坡500L计划；SV-DEMO-CLEAN及其7天前已验收清洗（62kW@800W/m²）；第2天61kW稳定复访。历史任务只展示既有交接，不重复扣当前水额。新增两表，无旧表字段变更；既有数据在自动建表后保留。固定200条模型参考样本不因复访改写。

演示续篇：计划600L送水→现场实收580L→主管入额，西坡配额600→1180L；第9天复访50kW@800W/m²，相对62kW基线衰减19.35%→登记新的同阵列巡测→主管关联新巡测、记录遮挡处置后关闭。扩展真实验收和4张新截图位于源码外 `test-artifacts/gb-1024/expansion/`。


## 第三轮扩展：许可、废水与结算

保留原6页，增加班组作业许可、废水去向交接、节水合同结算3页，共9个业务页面。

- 班组具有日分钟容量与启用状态。计划员给草稿任务申请许可，主管核阵风≤10m/s、隔离完成及日容量后签发；新派工必须消费已签许可，既有accepted任务无需补证。draft许可可修订风速、隔离、日期及工时；签发后冻结，派工前主管可撤销释放容量，撤销为终态。
- 废水绑定已验收任务，使用原任务水量快照，回收不得超用水；回收率不足70%主管核验前须有至少5字原因。recorded阶段可补正数量和原因，核验后冻结；现场签收人和接收凭证齐全才能交接，重复交接拒绝。
- 结算只能引用已验收且已交接废水的任务。收入=验收增益kW×合同单价，水成本=任务用水L×每升成本，毛利自动计算；负毛利须说明，主管签结冻结。保存阵列、巡测、验收增益、水量及废水接收凭据快照，仅生成本地合同凭据。

新增实体：WashCrew、WorkPermit（mission唯一）、WastewaterRecord（mission/manifest唯一）、WashSettlement（mission唯一），均有真实外键与数量约束。GET /crews /permits /wastewater /settlements供三角色查看；POST /crews及PUT /crews/{id}仅主管；POST/PUT /permits、POST/PUT /wastewater和POST /settlements允许计划员/主管；许可sign/void、废水verify、结算sign仅主管；废水handover允许计划员/主管。

容量签发按Mission→Permit→Crew加锁，锁读刷新Session缓存；MySQL事务使用READ COMMITTED，Crew行锁内计算已提交signed/used占用量，避免等待前建立的旧快照。派工许可消费与原水量扣减同事务，失败整笔回滚。种子只缺失时新增CREW-SOUTH/CREW-RIDGE，保留人工编辑和历史签结。

新增代码按实体、输入约束、路由、许可/废水/结算职责及3个页面组织。历史v1与expansion证据保留，第三轮证据在test-artifacts/gb-1024/v3/。作业许可按日容量预留，不模拟逐分钟班次；本地废水联单不接环保系统，合同凭据不触发收付款。

V3 已知范围补充：每任务一张作业许可；撤销为终止办理，需重新创建任务，当前不支持同任务重办许可。草稿许可可在签发前修订；撤销会释放班组容量。


## 第四轮扩展：跨日分段执行与剩余工时预测

山地清洗常因收工跨日，旧口径把许可全部工时记在派工日。本轮登记**分段实耗**并按班次认账，页面「跨日分段执行」集中办理执行、暂停、待接单与续作（共10个业务页面）。

- **分段实耗 `WorkSegment`**：每段记录认账许可、班组、作业日期、实耗分钟与实耗水量，分段单号唯一；同一许可每日唯一分段，同一任务同一作业日只认一个班次（换班并发时禁止两方重复认账）。累计实耗水量不得超过任务计划水量。迟报的实耗仍归原班组原日期，并即时重算该班组当日可用工时。
- **暂停保留**：暂停后初始许可 `used→paused`、任务 `execution_state=active→paused`；已耗水与已耗工时原样保留，原班组未耗工时（许可分钟−实耗）即时释放。暂停态禁止复测与验收。
- **次日续作换班**：可指定新班组申请 `continuation` 续作许可（挂 `parent_id`），新班组主管承接（approve）后才占容量并恢复作业；续作派工沿用任务已扣水量预算，**不重复扣水**。
- **容量与待接单**：班组容量同时按「预留（signed/used 许可分钟）」与「实耗（分段分钟）」核算，实耗超过日容量即超额，直接阻止该班组同日新签许可。续作申请时容量不足则许可保留 `pending` 待接单：不扣水、不占工时；中断后重试不重复占额（同任务 pending/signed 续作唯一、承接审批幂等）。
- **剩余工时预测 `duration_model.py`**：以已完工（accepted）且带分段实耗的任务为训练样本，特征为装机容量、面积、坡度、计划水量、目测积灰（不用实耗本身），RandomForest 100树/max_depth8/random_state42，按25%独立留出集报告留出 MAE 与百分比，训练 MAE 与留出 MAE 分开返回。已完工样本少于12条（划分后训练少于8条）不出预测，续作余量转人工核定，证据写入许可 `prediction_evidence`。
- 验收时未消费的在办许可作废；暂停许可若已挂分段实耗则恢复 `used` 作为已消费凭证。

新增接口：`GET/POST /segments`、`POST /missions/{id}/pause`、`POST /continuations`、`POST /continuations/{id}/approve|reject|resume`、`GET /missions/{id}/duration-prediction`。计划员/主管可登记分段、暂停、申请与恢复续作；承接审批仅主管；观察员只读。代码为 `execution_models.py/execution_schemas.py/execution_routes.py/execution_service.py/duration_model.py/schema_upgrade.py`，新页面 `pages/execution.js`。

结构升级：`work_permits.mission_id` 由唯一改为普通索引（同任务允许初始+多张续作许可），新增 `kind/parent_id/predicted_minutes/prediction_evidence`；`wash_missions` 新增 `execution_state`；新建 `work_segments` 表（历史无凭证任务的 `permit_id` 可空）。`schema_upgrade.py` 在启动 `create_all` 后幂等补列/删旧唯一索引，无在线迁移工具，既有数据保留。种子新增18条已完工训练任务（含跨日双段）及历史任务补录分段，按自然键幂等。

容量加锁沿用 Mission→Permit→Crew 顺序与 READ COMMITTED，行锁内重算预留与实耗。验证证据在 `test-artifacts/gb-1024/v4/`：服务层7项（分段/超水、暂停释放、换班待接单不扣水、迟报超额阻签、暂停阻复测、人工核定→留出MAE、一班次认账护栏）、HTTP全链路剧本、v3旧库→v4幂等升级三套测试，基于 SQLite 内存库与 TestClient，不依赖 Docker/MySQL/Redis。
