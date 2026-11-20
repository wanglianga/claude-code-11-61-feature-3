<template>
  <div>
    <div v-if="error" class="panel panel-body"><div class="notice danger">{{ error }}</div></div>
    <template v-else>
      <section class="panel">
        <div class="panel-head">
          <h2>基线不确定度分析</h2>
          <span class="small muted">伪活动日交叉拟合残差 → 预测区间；只报告区间，不改写基线、不宣称实际削减</span>
          <div style="flex:1"></div>
          <select v-model="selectedRunId" style="width:260px">
            <option v-for="r in runs" :key="r.id" :value="r.id">
              {{ r.run_code }} · v{{ r.version }} · {{ STATUS_LABEL[r.status] || r.status }}{{ r.published_at ? ' · 已发布' : '' }}
            </option>
          </select>
        </div>
        <div class="panel-body" v-if="reports.length">
          <div class="grid cols-4">
            <div class="stat"><div class="label">分析窗口</div><div class="value">{{ reports.length }}</div><div class="hint">每个客户活动窗口一份报告</div></div>
            <div class="stat"><div class="label">充分 / 降级 / 无法估计</div><div class="value">{{ counts.ok }} / {{ counts.degraded }} / {{ counts.unavailable }}</div><div class="hint">降级与无法估计均不阻断结算</div></div>
            <div class="stat"><div class="label">平均相对半宽</div><div class="value">{{ avgRelativeWidth }}</div><div class="hint">区间半宽 ÷ 基线电量（仅可估计窗口）</div></div>
            <div class="stat"><div class="label">置信水平</div><div class="value">{{ levelPct }}</div><div class="hint">同一锁定规则与输入版本（截止 {{ fmt.dt(run?.cutoff_at) }}）</div></div>
          </div>
        </div>
      </section>

      <section class="panel" style="margin-top:14px">
        <div class="panel-head">
          <h3>窗口预测区间</h3>
          <div style="flex:1"></div>
          <select v-model="statusFilter" style="width:150px">
            <option value="">全部状态</option>
            <option value="ok">充分</option>
            <option value="degraded">降级</option>
            <option value="unavailable">无法估计</option>
          </select>
        </div>
        <table>
          <thead>
            <tr>
              <th>客户</th><th>活动</th><th>状态</th>
              <th class="num">基线 (kWh)</th><th class="num">预测区间 (kWh)</th><th class="num">半宽</th>
              <th>相对半宽</th><th class="num">合格/伪活动日</th><th>星期覆盖</th><th>天气覆盖</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in filteredReports" :key="r.id" class="clickable" @click="openDetail(r)">
              <td><strong>{{ r.customer_code }}</strong><div class="small muted">{{ r.customer_name }}</div></td>
              <td class="small">{{ r.event_code }}<div class="muted">{{ r.event_date }}</div></td>
              <td><span class="badge" :class="uncBadge(r.status)">{{ uncLabel(r.status) }}</span></td>
              <td class="num">{{ r.baseline_kwh ?? '—' }}</td>
              <td class="num">
                <span v-if="r.lower_kwh != null">[{{ r.lower_kwh }}, {{ r.upper_kwh }}]</span>
                <span v-else>—</span>
              </td>
              <td class="num">{{ r.half_width_kwh ?? '—' }}</td>
              <td style="min-width:110px">
                <template v-if="r.relative_width != null">
                  <div class="bar"><span :style="{ width: Math.min(100, r.relative_width * 100 * 2) + '%', background: widthColor(r.relative_width) }"></span></div>
                  <span class="small muted">{{ (r.relative_width * 100).toFixed(1) }}%</span>
                </template>
                <span v-else>—</span>
              </td>
              <td class="num">{{ r.qualified_days }} / {{ r.pseudo_days }}</td>
              <td><span class="badge" :class="r.weekday_covered ? 'ok' : 'warn'">{{ r.weekday_covered ? '覆盖' : '未覆盖' }}</span></td>
              <td><span class="badge" :class="weatherBadge(r.weather_status)">{{ weatherLabel(r.weather_status) }}</span></td>
              <td><button class="btn small">详情</button></td>
            </tr>
          </tbody>
        </table>
        <div v-if="!filteredReports.length" class="empty">该运行暂无不确定度报告（请先运行一次结算）</div>
      </section>

      <section v-if="detail" class="panel" style="margin-top:14px">
        <div class="panel-head">
          <h2>{{ detail.customer_code }} × {{ detail.event_code }}（{{ detail.event_date }}）</h2>
          <span class="badge" :class="uncBadge(detail.status)">{{ uncLabel(detail.status) }}</span>
          <div style="flex:1"></div>
          <button class="btn small" @click="detail = null">收起</button>
        </div>
        <div class="panel-body">
          <div v-if="detail.unavailable_reasons?.length" class="notice warn" style="margin-bottom:12px">
            无法估计：{{ reasonText(detail.unavailable_reasons) }}。不确定度只报告不设卡，结算不受影响。
          </div>
          <div v-else-if="detail.degraded_reasons?.length" class="notice info" style="margin-bottom:12px">
            降级提示：{{ reasonText(detail.degraded_reasons) }}
          </div>

          <div class="grid cols-3">
            <div class="panel"><div class="panel-head"><h3>预测区间（{{ (detail.level * 100).toFixed(0) }}% 置信）</h3></div>
              <div class="panel-body">
                <dl class="kv">
                  <dt>基线电量</dt><dd>{{ detail.baseline_kwh ?? '—' }} kWh</dd>
                  <dt>区间下/上限</dt><dd>{{ detail.interval ? `${detail.interval.lower_kwh} ~ ${detail.interval.upper_kwh} kWh` : '—' }}</dd>
                  <dt>半宽 / 相对</dt><dd>{{ detail.interval ? `${detail.interval.half_width_kwh} kWh（${(detail.interval.relative_width * 100).toFixed(1)}%）` : '—' }}</dd>
                  <dt>主方法</dt><dd>留出交叉拟合（holdout）</dd>
                </dl>
              </div>
            </div>
            <div class="panel"><div class="panel-head"><h3>区间宽度来源</h3></div>
              <div class="panel-body">
                <dl class="kv">
                  <dt>残差噪声</dt><dd>{{ wc.residual_noise_kwh ?? '—' }} kWh（主要来源）</dd>
                  <dt>样本量因子</dt><dd>×{{ wc.sample_size_factor }}（基线样本 {{ detail.samples?.baseline_sample_days }} 天）</dd>
                  <dt>覆盖因子</dt><dd>×{{ wc.coverage_factor }}（星期 ×{{ wc.weekday_factor }} · 天气 ×{{ wc.weather_factor }}）</dd>
                  <dt>最终半宽</dt><dd><strong>{{ wc.final_half_width_kwh ?? '—' }} kWh</strong></dd>
                </dl>
                <div class="small muted" style="margin-top:8px">{{ (wc.notes || []).join('；') }}</div>
              </div>
            </div>
            <div class="panel"><div class="panel-head"><h3>星期 / 天气覆盖</h3></div>
              <div class="panel-body">
                <div style="display:flex;gap:4px;margin-bottom:10px">
                  <span v-for="(label, i) in weekdayNames" :key="i" class="badge"
                    :class="i === weekdayCov.event_weekday ? 'info' : (weekdayCov.sample_weekdays || []).includes(i) ? 'ok' : 'gray'">
                    {{ label }}{{ i === weekdayCov.event_weekday ? '·活动' : '' }}
                  </span>
                </div>
                <dl class="kv">
                  <dt>天气覆盖</dt><dd><span class="badge" :class="weatherBadge(weatherCov.status)">{{ weatherLabel(weatherCov.status) }}</span></dd>
                  <dt>活动日气温</dt><dd>{{ weatherCov.event_temp_c != null ? weatherCov.event_temp_c + ' ℃' : '缺项' }}</dd>
                  <dt>样本气温范围</dt><dd>{{ weatherCov.sample_temp_min_c != null ? `${weatherCov.sample_temp_min_c} ~ ${weatherCov.sample_temp_max_c} ℃` : '—' }}</dd>
                  <dt>天气缺项日</dt><dd class="small">{{ (weatherCov.missing_weather_days || []).join(', ') || '无' }}</dd>
                </dl>
              </div>
            </div>
          </div>

          <div class="grid cols-2" style="margin-top:14px">
            <div class="panel"><div class="panel-head"><h3>校准诊断 · 留出交叉拟合</h3><span class="badge" :class="uncBadge(methods.holdout?.status)">{{ uncLabel(methods.holdout?.status) }}</span></div>
              <div class="panel-body">
                <dl class="kv">
                  <dt>拟合残差</dt><dd>{{ methods.holdout?.fit?.n ?? 0 }} 天（留出 {{ methods.holdout?.holdout?.n ?? 0 }} 天验证）</dd>
                  <dt>残差偏置/标准差</dt><dd>{{ methods.holdout?.fit?.bias_kwh ?? '—' }} / {{ methods.holdout?.fit?.std_kwh ?? '—' }} kWh</dd>
                  <dt>一阶自相关</dt><dd>{{ methods.holdout?.fit?.lag1_autocorr ?? '—' }}</dd>
                  <dt>留出段覆盖率</dt><dd>
                    <strong v-if="methods.holdout?.holdout?.empirical_coverage != null">
                      {{ (methods.holdout.holdout.empirical_coverage * 100).toFixed(0) }}%（目标 {{ (detail.level * 100).toFixed(0) }}%）
                    </strong>
                    <span v-else>—</span>
                  </dd>
                  <dt>留出段代表性</dt><dd class="small">{{ holdoutRepresentative }}</dd>
                </dl>
                <table v-if="methods.holdout?.holdout?.days?.length" style="margin-top:8px">
                  <thead><tr><th>留出日</th><th class="num">实测</th><th class="num">区间</th><th>覆盖</th></tr></thead>
                  <tbody>
                    <tr v-for="d in methods.holdout.holdout.days" :key="d.day">
                      <td class="small">{{ d.day }}</td><td class="num">{{ d.actual_kwh }}</td>
                      <td class="num small">[{{ d.interval[0] }}, {{ d.interval[1] }}]</td>
                      <td><span :class="d.covered ? 'check' : 'cross'">{{ d.covered ? '✓' : '✗' }}</span></td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
            <div class="panel"><div class="panel-head"><h3>校准诊断 · 连续日期块重采样</h3><span class="badge" :class="uncBadge(methods.block_bootstrap?.status)">{{ uncLabel(methods.block_bootstrap?.status) }}</span></div>
              <div class="panel-body">
                <dl class="kv">
                  <dt>相似日池</dt><dd>{{ methods.block_bootstrap?.pool?.n_days ?? 0 }} 天（{{ similarityLabel }}）</dd>
                  <dt>连续日期块</dt><dd>{{ methods.block_bootstrap?.pool?.n_blocks ?? 0 }} 块 × {{ methods.block_bootstrap?.pool?.block_length ?? 3 }} 天</dd>
                  <dt>重采样次数</dt><dd>{{ methods.block_bootstrap?.replicates ?? 0 }}</dd>
                  <dt>半宽 P10/P50/P90</dt><dd class="num">{{ bootstrapHalfWidth }}</dd>
                  <dt>季节迁移</dt><dd>{{ seasonDrift }}</dd>
                </dl>
                <div v-if="methods.block_bootstrap?.warnings?.length" class="notice warn small" style="margin-top:8px">
                  {{ reasonText(methods.block_bootstrap.warnings) }}
                </div>
              </div>
            </div>
          </div>

          <div class="panel" style="margin-top:14px"><div class="panel-head"><h3>伪活动日预测残差（{{ pseudoRows.length }} 天）</h3><span class="small muted">每个合格非活动日：排除自身及泄漏窗口后按锁定规则重算预测，与实测比较</span></div>
            <div class="panel-body" style="padding:0">
              <table>
                <thead><tr><th>日期</th><th>星期</th><th class="num">气温</th><th class="num">实测 (kWh)</th><th class="num">预测 (kWh)</th><th class="num">残差 (kWh)</th><th>残差分布</th><th class="num">样本天数</th><th>泄漏窗口</th></tr></thead>
                <tbody>
                  <tr v-for="p in pseudoRows" :key="p.day">
                    <td class="small">{{ p.day }}</td>
                    <td class="small">{{ weekdayNames[p.weekday] }}</td>
                    <td class="num">{{ p.temp_c ?? '—' }}</td>
                    <td class="num">{{ p.actual_kwh }}</td>
                    <td class="num">{{ p.predicted_kwh }}</td>
                    <td class="num" :style="{ color: p.residual_kwh < 0 ? 'var(--danger)' : 'var(--ok)' }">{{ p.residual_kwh }}</td>
                    <td style="min-width:120px"><div class="bar"><span :style="{ width: residualBarWidth(p.residual_kwh) + '%', marginLeft: p.residual_kwh < 0 ? 'auto' : '0', background: p.residual_kwh < 0 ? 'var(--danger)' : 'var(--ok)' }"></span></div></td>
                    <td class="num">{{ p.sample_days }}</td>
                    <td class="small muted">{{ p.leakage_window[0] }} ~ {{ p.leakage_window[p.leakage_window.length - 1] }}</td>
                  </tr>
                </tbody>
              </table>
              <div v-if="!pseudoRows.length" class="empty">无可用伪活动日</div>
            </div>
          </div>

          <div class="panel" style="margin-top:14px"><div class="panel-head"><h3>样本资格与排除理由（校准窗口 {{ detail.samples?.calibration_window?.start }} ~ {{ detail.samples?.calibration_window?.end }}）</h3></div>
            <div class="panel-body" style="padding:0;max-height:360px;overflow:auto">
              <table>
                <thead><tr><th>日期</th><th>角色</th><th>资格</th><th>排除理由</th><th class="num">窗口电量</th><th class="num">气温</th><th>残差</th></tr></thead>
                <tbody>
                  <tr v-for="row in eligibilityRows" :key="row.day">
                    <td class="small">{{ row.day }}（{{ weekdayNames[row.weekday] }}）</td>
                    <td class="small muted">{{ row.role === 'calibration' ? '校准窗口' : '回看样本' }}</td>
                    <td><span class="badge" :class="row.eligible ? 'ok' : 'gray'">{{ row.eligible ? '合格' : '排除' }}</span></td>
                    <td class="small" style="color:var(--warn)">{{ (row.reason_labels || []).join('、') }}<span v-if="row.prediction_unavailable" class="muted">（样本不足无法预测）</span></td>
                    <td class="num">{{ row.day_total_kwh ?? '—' }}</td>
                    <td class="num">{{ row.temp_c ?? '—' }}</td>
                    <td class="num">{{ row.pseudo ? row.pseudo.residual_kwh : '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <div class="panel" style="margin-top:14px"><div class="panel-head"><h3>锁定输入与声明</h3></div>
            <div class="panel-body">
              <dl class="kv">
                <dt>规则版本</dt><dd>规则 #{{ detail.input_refs?.rule_id }} v{{ detail.input_refs?.rule_version }}（与基线节点同一锁定版本）</dd>
                <dt>输入截止</dt><dd>{{ fmt.dt(detail.run_cutoff_at) }}</dd>
                <dt>引用来源</dt><dd class="small">气象 {{ (detail.input_refs?.weather_ids || []).length }} 行 · 节假日 {{ (detail.input_refs?.holiday_ids || []).length }} 行 · 停电/限电 {{ (detail.input_refs?.outage_ids || []).length }} 行 · 批次 {{ (detail.input_refs?.batch_ids || []).length }} 个</dd>
                <dt>校准参数</dt><dd class="small">校准窗口 {{ detail.input_refs?.params?.calibration_lookback_days }} 天 · 最少残差 {{ detail.input_refs?.params?.min_residuals }} · 块长 {{ detail.input_refs?.params?.block_length }} 天 · 重采样 {{ detail.input_refs?.params?.bootstrap_replicates }} 次</dd>
                <dt>声明</dt><dd class="small" style="color:var(--warn)">{{ detail.report?.disclaimer }}</dd>
              </dl>
            </div>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api, fmt, STATUS_LABEL } from '../api'
import { periodStore } from '../periodStore'

const runs = ref([])
const selectedRunId = ref(null)
const run = ref(null)
const reports = ref([])
const detail = ref(null)
const error = ref('')
const statusFilter = ref('')

const weekdayNames = ['一', '二', '三', '四', '五', '六', '日']

const UNC_LABEL = { ok: '充分', degraded: '降级', unavailable: '无法估计' }
const UNC_BADGE = { ok: 'ok', degraded: 'warn', unavailable: 'danger' }
const REASON_LABEL = {
  no_applicable_rule: '活动日无生效基线规则版本',
  baseline_unavailable: '基线节点未产出数值',
  qualified_samples_insufficient: '合格伪活动日样本不足（<5 天）',
  qualified_samples_low: '合格伪活动日样本偏少（5–7 天）',
  weather_missing: '天气缺项',
  weather_partial: '部分样本日缺少气象数据',
  holdout_not_independent: '留出段与拟合段不独立',
  similarity_pool_fallback: '相似非活动日不足，回退到全部合格日',
  blocks_insufficient: '连续日期块过少，块重采样不稳定',
  season_drift: '校准窗口内负荷存在季节迁移趋势',
}

const counts = computed(() => {
  const c = { ok: 0, degraded: 0, unavailable: 0 }
  for (const r of reports.value) c[r.status] = (c[r.status] || 0) + 1
  return c
})

const avgRelativeWidth = computed(() => {
  const vals = reports.value.filter((r) => r.relative_width != null).map((r) => r.relative_width)
  if (!vals.length) return '—'
  return `${((vals.reduce((a, b) => a + b, 0) / vals.length) * 100).toFixed(1)}%`
})

const levelPct = computed(() => (reports.value.length ? `${(reports.value[0].level * 100).toFixed(0)}%` : '—'))

const filteredReports = computed(() =>
  statusFilter.value ? reports.value.filter((r) => r.status === statusFilter.value) : reports.value,
)

const wc = computed(() => detail.value?.report?.width_components || {})
const methods = computed(() => detail.value?.report?.methods || {})
const weekdayCov = computed(() => detail.value?.report?.samples?.weekday_coverage || {})
const weatherCov = computed(() => detail.value?.report?.samples?.weather_coverage || {})
const eligibilityRows = computed(() => detail.value?.report?.samples?.eligibility || [])
const pseudoRows = computed(() => eligibilityRows.value.filter((r) => r.pseudo).map((r) => r.pseudo))

const holdoutRepresentative = computed(() => {
  const h = methods.value.holdout?.holdout
  if (!h || h.representative == null) return '无法判定（气温缺项）'
  return h.representative
    ? `有代表性（留出段均温 ${h.temp_mean_c}℃ vs 活动日 ${h.event_temp_c}℃）`
    : `代表性弱（留出段均温 ${h.temp_mean_c}℃ vs 活动日 ${h.event_temp_c}℃）`
})

const similarityLabel = computed(() => {
  const s = methods.value.block_bootstrap?.pool?.similarity
  return s === 'weekday+weather' ? '星期+天气相似' : '仅星期相似'
})

const bootstrapHalfWidth = computed(() => {
  const h = methods.value.block_bootstrap?.half_width_kwh
  return h ? `${h.p10} / ${h.p50} / ${h.p90} kWh` : '—'
})

const seasonDrift = computed(() => {
  const d = methods.value.block_bootstrap?.season_drift
  if (!d || d.drift_ratio == null) return '样本不足，未评估'
  const pct = `${(d.drift_ratio * 100).toFixed(1)}%`
  return d.drift_ratio > d.threshold ? `明显（${pct} > 阈值 ${(d.threshold * 100).toFixed(0)}%）` : `平稳（${pct}）`
})

function uncLabel(s) { return UNC_LABEL[s] || s || '—' }
function uncBadge(s) { return UNC_BADGE[s] || 'gray' }
function reasonText(codes) { return (codes || []).map((c) => REASON_LABEL[c] || c).join('；') }
function weatherLabel(s) { return { ok: '覆盖', partial: '部分缺项', missing: '天气缺项' }[s] || '—' }
function weatherBadge(s) { return { ok: 'ok', partial: 'warn', missing: 'danger' }[s] || 'gray' }
function widthColor(w) { return w < 0.1 ? 'var(--ok)' : w < 0.25 ? 'var(--warn)' : 'var(--danger)' }

function residualBarWidth(v) {
  const max = Math.max(...pseudoRows.value.map((p) => Math.abs(p.residual_kwh)), 1e-9)
  return Math.min(100, (Math.abs(v) / max) * 100)
}

async function loadRuns() {
  error.value = ''
  try {
    const data = await api('/runs', { params: { period_month: periodStore.period, limit: 50 } })
    runs.value = data.filter((r) => r.status !== 'failed' && r.status !== 'running')
    if (runs.value.length && !selectedRunId.value) {
      const published = runs.value.find((r) => r.published_at)
      selectedRunId.value = (published || runs.value[0]).id
    }
  } catch (e) {
    error.value = e.message
  }
}

async function loadReports() {
  if (!selectedRunId.value) return
  detail.value = null
  try {
    const data = await api(`/runs/${selectedRunId.value}/uncertainty`)
    run.value = data.run
    reports.value = data.reports || []
  } catch (e) {
    error.value = e.message
  }
}

async function openDetail(r) {
  try {
    detail.value = await api(`/uncertainty/${r.id}`)
  } catch (e) {
    error.value = e.message
  }
}

watch(selectedRunId, loadReports)
watch(() => periodStore.period, () => { selectedRunId.value = null; loadRuns() })
onMounted(loadRuns)
</script>
