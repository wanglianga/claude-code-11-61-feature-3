<template>
  <div class="panel">
    <div class="panel-head">
      <h2>来源登记</h2><span class="small muted">批次 / 规则版本 / 签约版本均不可改写，修订生成新版本</span>
      <div style="flex:1"></div>
      <button class="btn small" @click="loadAll" :disabled="loading">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>
    <div class="tabs" style="padding:0 16px">
      <button v-for="t in tabs" :key="t.key" :class="{ active: tab === t.key }" @click="tab = t.key">{{ t.label }}</button>
    </div>
    <div v-if="error" class="panel-body"><div class="notice danger">{{ error }}</div></div>
    <div v-else class="panel-body">

      <!-- batches -->
      <div v-if="tab === 'batches'">
        <div class="grid cols-2">
          <section class="panel">
            <div class="panel-head"><h3>已登记批次</h3><div style="flex:1"></div><span class="small muted">最近 {{ batches.length }} 条</span></div>
            <div style="max-height:520px;overflow:auto">
              <table>
                <thead><tr><th>批次</th><th>计量点</th><th class="num">版本</th><th>覆盖区间</th><th class="num">电量</th><th>登记时间</th></tr></thead>
                <tbody>
                  <tr v-for="b in batches" :key="b.id">
                    <td class="mono">#{{ b.id }} <span class="muted small">{{ b.checksum }}</span></td>
                    <td>{{ b.meter_no }}<div class="small muted">{{ b.customer_name }}</div></td>
                    <td class="num">v{{ b.version }}<div v-if="b.version > 1" class="small" style="color:var(--warn)">补传</div></td>
                    <td class="small">{{ b.coverage_start?.slice(0, 10) }} → {{ b.coverage_start?.slice(11, 16) }}</td>
                    <td class="num">{{ b.total_kwh }}</td>
                    <td class="small">{{ fmt.dt(b.submitted_at) }}</td>
                  </tr>
                </tbody>
              </table>
              <div v-if="!batches.length" class="empty">暂无批次</div>
            </div>
          </section>
          <section class="panel">
            <div class="panel-head"><h3>补传批次</h3></div>
            <div class="panel-body">
              <div class="notice info small" style="margin-bottom:12px">
                提交与既有批次内容不同的新批次会生成新版本（v+1），旧版本保留；
                相同内容会返回 409。每笔 15 分钟区间必须连续无缺口。
              </div>
              <div class="toolbar" style="margin-bottom:10px">
                <div class="field" style="flex:1;min-width:160px">
                  <label>计量点</label>
                  <select v-model="batchForm.meter_no">
                    <option v-for="m in meters" :key="m.id" :value="m.meter_no">{{ m.meter_no }} · {{ m.customer_code }} {{ m.customer_name }}</option>
                  </select>
                </div>
                <div class="field" style="flex:1;min-width:160px">
                  <label>日期</label>
                  <input v-model="batchForm.day" type="date" />
                </div>
              </div>
              <div class="field" style="margin-bottom:10px">
                <label>批量替换窗口内读数（kWh/15min）——仅用于演示补传</label>
                <div class="toolbar">
                  <input v-model.number="batchForm.low" type="number" step="0.1" placeholder="窗口内低值" style="width:130px" />
                  <input v-model.number="batchForm.high" type="number" step="0.1" placeholder="窗口外高值" style="width:130px" />
                  <input v-model="batchForm.note" placeholder="来源说明" style="flex:1;min-width:140px" />
                </div>
              </div>
              <button class="btn primary" :disabled="!canPlan() || submitting || !batchForm.meter_no" @click="submitBatch">
                {{ submitting ? '提交中…' : '登记补传批次' }}
              </button>
              <div v-if="!canPlan()" class="small muted" style="margin-top:8px">只读角色不能登记来源</div>
              <div v-if="result" class="notice" :class="result.ok ? 'ok' : 'danger'" style="margin-top:10px">
                {{ result.message }}
              </div>
              <div v-if="impact" class="panel" style="margin-top:12px">
                <div class="panel-head"><h3>该计量点的候选窗口</h3></div>
                <table>
                  <thead><tr><th>活动</th><th>日期</th><th>候选客户</th></tr></thead>
                  <tbody>
                    <tr v-for="w in impact.candidate_windows" :key="w.event_id">
                      <td class="mono">{{ w.event_code }}</td><td>{{ w.event_date }}</td>
                      <td class="small">{{ w.customer_ids.join(', ') }}</td>
                    </tr>
                  </tbody>
                </table>
                <div class="small muted" style="padding:8px 12px">{{ impact.note }}</div>
              </div>
            </div>
          </section>
        </div>
      </div>

      <!-- rules -->
      <div v-if="tab === 'rules'">
        <div class="grid cols-2">
          <section class="panel">
            <div class="panel-head"><h3>基线规则版本</h3></div>
            <table>
              <thead><tr><th>规则</th><th class="num">版本</th><th class="num">回看/最少样本</th><th class="num">调整系数</th><th>生效</th><th>原因</th></tr></thead>
              <tbody>
                <tr v-for="r in rules" :key="r.id">
                  <td class="mono">{{ r.rule_code }}</td>
                  <td class="num">v{{ r.version }}</td>
                  <td class="num">{{ r.lookback_days }} / {{ r.min_sample_days }}</td>
                  <td class="num">{{ r.adjustment_factor }}</td>
                  <td class="small">{{ r.effective_from }}<span v-if="r.effective_to"> → {{ r.effective_to }}</span></td>
                  <td class="small muted">{{ r.reason }}</td>
                </tr>
              </tbody>
            </table>
            <div v-if="!rules.length" class="empty">暂无规则</div>
          </section>
          <section class="panel">
            <div class="panel-head"><h3>登记规则修订版本</h3></div>
            <div class="panel-body">
              <div class="notice info small" style="margin-bottom:12px">
                修订生成新版本；活动日按「生效起始日 ≤ 活动日」选取适用版本。样本不足时下游会停在待核，不会零值结算。
              </div>
              <div class="grid cols-2">
                <div class="field"><label>规则编码</label><input v-model="ruleForm.rule_code" /></div>
                <div class="field"><label>生效起始日</label><input v-model="ruleForm.effective_from" type="date" /></div>
                <div class="field"><label>回看天数</label><input v-model.number="ruleForm.lookback_days" type="number" min="1" /></div>
                <div class="field"><label>最少样本天数</label><input v-model.number="ruleForm.min_sample_days" type="number" min="1" /></div>
                <div class="field"><label>调整系数</label><input v-model.number="ruleForm.adjustment_factor" type="number" step="0.01" /></div>
                <div class="field"><label>修订原因</label><input v-model="ruleForm.reason" /></div>
              </div>
              <div style="display:flex;gap:10px;margin-top:12px">
                <button class="btn primary" :disabled="!canPlan() || submitting" @click="submitRule">登记规则版本</button>
                <button class="btn" :disabled="submitting" @click="validateRule">预检样本风险</button>
              </div>
              <div v-if="ruleValidation" class="notice" :class="ruleValidation.at_risk.length ? 'warn' : 'ok'" style="margin-top:10px">
                回看 {{ ruleValidation.lookback_days }} 天，要求 ≥{{ ruleValidation.min_sample_days }} 天；
                <template v-if="ruleValidation.at_risk.length">
                  有 {{ ruleValidation.at_risk.length }} 个客户会样本不足：
                  {{ ruleValidation.at_risk.map((c) => `${c.customer_code}(${c.available_days}天)`).join('、') }}
                </template>
                <template v-else>全部客户满足样本要求。</template>
              </div>
              <div v-if="result" class="notice" :class="result.ok ? 'ok' : 'danger'" style="margin-top:10px">{{ result.message }}</div>
            </div>
          </section>
        </div>
      </div>

      <!-- contracts / customers -->
      <div v-if="tab === 'contracts'">
        <div class="grid cols-2">
          <section class="panel">
            <div class="panel-head"><h3>客户与现行签约</h3></div>
            <table>
              <thead><tr><th>客户</th><th>区域</th><th class="num">计量点</th><th class="num">签约容量 kW</th><th class="num">版本</th><th>生效</th></tr></thead>
              <tbody>
                <tr v-for="c in customers" :key="c.id">
                  <td><strong>{{ c.code }}</strong><div class="small muted">{{ c.name }}</div></td>
                  <td class="small">{{ c.region }}</td>
                  <td class="num">{{ c.meters }}</td>
                  <td class="num">{{ c.latest_contract?.capacity_kw ?? '—' }}</td>
                  <td class="num">v{{ c.latest_contract?.version ?? '—' }}</td>
                  <td class="small">{{ c.latest_contract?.effective_from ?? '—' }}</td>
                </tr>
              </tbody>
            </table>
          </section>
          <section class="panel">
            <div class="panel-head"><h3>登记签约修订</h3></div>
            <div class="panel-body">
              <div class="notice info small" style="margin-bottom:12px">
                若原账本已付款，重算差额不会改写原凭证，而是生成关联调整记录，待审批后单独出具调整凭证。
              </div>
              <div class="grid cols-2">
                <div class="field">
                  <label>客户</label>
                  <select v-model.number="contractForm.customer_id">
                    <option v-for="c in customers" :key="c.id" :value="c.id">{{ c.code }} · {{ c.name }}</option>
                  </select>
                </div>
                <div class="field"><label>签约容量 kW</label><input v-model.number="contractForm.capacity_kw" type="number" step="1" /></div>
                <div class="field"><label>生效日</label><input v-model="contractForm.effective_from" type="date" /></div>
                <div class="field"><label>修订原因</label><input v-model="contractForm.reason" /></div>
              </div>
              <button class="btn primary" style="margin-top:12px" :disabled="!canPlan() || submitting" @click="submitContract">登记签约版本</button>
              <div v-if="result" class="notice" :class="result.ok ? 'ok' : 'danger'" style="margin-top:10px">{{ result.message }}</div>
            </div>
          </section>
        </div>
        <section class="panel" style="margin-top:14px">
          <div class="panel-head"><h3>签约版本历史</h3></div>
          <table>
            <thead><tr><th>客户</th><th class="num">版本</th><th class="num">容量 kW</th><th>生效</th><th>原因</th><th>登记人</th><th>登记时间</th></tr></thead>
            <tbody>
              <tr v-for="c in contracts" :key="c.id">
                <td>{{ c.customer_code }}</td><td class="num">v{{ c.version }}</td>
                <td class="num">{{ c.capacity_kw }}</td><td>{{ c.effective_from }}</td>
                <td class="small muted">{{ c.reason }}</td><td class="small">{{ c.created_by }}</td>
                <td class="small">{{ fmt.dt(c.created_at) }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>

      <!-- weather / holidays / outages -->
      <div v-if="tab === 'calendar'">
        <div class="notice info small" style="margin-bottom:14px">
          气象日值、节假日与停电/限电记录是基线不确定度分析的输入来源：同样只追加不覆写，气象修订生成新版本；
          节假日与停电/限电日会从基线样本与伪活动日中排除（保留排除理由），天气缺项会触发覆盖诊断降级。
        </div>
        <div class="grid cols-3">
          <section class="panel">
            <div class="panel-head"><h3>气象日值（按区域）</h3></div>
            <div class="panel-body" style="padding:0;max-height:260px;overflow:auto">
              <table>
                <thead><tr><th>区域</th><th>日期</th><th class="num">版本</th><th class="num">均温℃</th><th class="num">最高℃</th></tr></thead>
                <tbody>
                  <tr v-for="w in weather" :key="w.id">
                    <td class="small">{{ w.region }}</td><td class="small">{{ w.day }}</td>
                    <td class="num">v{{ w.version }}</td><td class="num">{{ w.avg_temp_c }}</td>
                    <td class="num">{{ w.max_temp_c ?? '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="panel-body" style="border-top:1px solid var(--line)">
              <div class="field"><label>区域</label>
                <select v-model="weatherForm.region"><option v-for="r in regions" :key="r" :value="r">{{ r }}</option></select>
              </div>
              <div class="field" style="margin-top:8px"><label>日期</label><input v-model="weatherForm.day" type="date" /></div>
              <div style="display:flex;gap:8px;margin-top:8px">
                <div class="field" style="flex:1"><label>均温 ℃</label><input v-model.number="weatherForm.avg_temp_c" type="number" step="0.1" /></div>
                <div class="field" style="flex:1"><label>最高 ℃</label><input v-model.number="weatherForm.max_temp_c" type="number" step="0.1" /></div>
              </div>
              <button class="btn primary small" style="margin-top:10px" :disabled="submitting || !canPlan()" @click="submitWeather">登记气象日值</button>
            </div>
          </section>

          <section class="panel">
            <div class="panel-head"><h3>节假日日历</h3></div>
            <div class="panel-body" style="padding:0;max-height:260px;overflow:auto">
              <table>
                <thead><tr><th>日期</th><th>名称</th><th>登记人</th></tr></thead>
                <tbody>
                  <tr v-for="h in holidays" :key="h.id">
                    <td class="small">{{ h.day }}</td><td class="small">{{ h.name }}</td><td class="small muted">{{ h.created_by }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="panel-body" style="border-top:1px solid var(--line)">
              <div class="field"><label>日期</label><input v-model="holidayForm.day" type="date" /></div>
              <div class="field" style="margin-top:8px"><label>名称</label><input v-model="holidayForm.name" placeholder="如：中秋节（虚构演示）" /></div>
              <button class="btn primary small" style="margin-top:10px" :disabled="submitting || !canPlan()" @click="submitHoliday">登记节假日</button>
            </div>
          </section>

          <section class="panel">
            <div class="panel-head"><h3>停电 / 限电日</h3></div>
            <div class="panel-body" style="padding:0;max-height:260px;overflow:auto">
              <table>
                <thead><tr><th>客户</th><th>日期</th><th>类型</th><th>备注</th></tr></thead>
                <tbody>
                  <tr v-for="o in outages" :key="o.id">
                    <td class="small">{{ o.customer_code }}</td><td class="small">{{ o.day }}</td>
                    <td><span class="badge" :class="o.kind === 'outage' ? 'danger' : 'warn'">{{ o.kind === 'outage' ? '停电' : '限电' }}</span></td>
                    <td class="small muted">{{ o.note }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div class="panel-body" style="border-top:1px solid var(--line)">
              <div class="field"><label>客户</label>
                <select v-model.number="outageForm.customer_id">
                  <option v-for="c in customers" :key="c.id" :value="c.id">{{ c.code }} · {{ c.name }}</option>
                </select>
              </div>
              <div style="display:flex;gap:8px;margin-top:8px">
                <div class="field" style="flex:1"><label>日期</label><input v-model="outageForm.day" type="date" /></div>
                <div class="field" style="flex:1"><label>类型</label>
                  <select v-model="outageForm.kind"><option value="outage">停电</option><option value="curtailment">限电</option></select>
                </div>
              </div>
              <div class="field" style="margin-top:8px"><label>备注</label><input v-model="outageForm.note" placeholder="如：园区计划检修" /></div>
              <button class="btn primary small" style="margin-top:10px" :disabled="submitting || !canPlan()" @click="submitOutage">登记停电/限电</button>
            </div>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, canPlan, fmt } from '../api'
import { periodStore } from '../periodStore'

const tabs = [
  { key: 'batches', label: '计量批次' },
  { key: 'rules', label: '基线规则' },
  { key: 'contracts', label: '客户与签约' },
  { key: 'calendar', label: '气象与日历' },
]
const tab = ref('batches')
const loading = ref(false)
const submitting = ref(false)
const error = ref('')
const result = ref(null)
const impact = ref(null)
const ruleValidation = ref(null)
const batches = ref([])
const rules = ref([])
const customers = ref([])
const contracts = ref([])
const meters = ref([])
const events = ref([])
const weather = ref([])
const holidays = ref([])
const outages = ref([])

const batchForm = ref({ meter_no: '', day: '', low: 20, high: 300, note: '补传更正批次' })
const ruleForm = ref({
  rule_code: 'BL-DEFAULT',
  effective_from: '2025-01-01',
  lookback_days: 5,
  min_sample_days: 3,
  adjustment_factor: 1.05,
  reason: '基线规则修订',
})
const contractForm = ref({ customer_id: 1, capacity_kw: 500, effective_from: '2025-08-01', reason: '签约容量修订' })
const weatherForm = ref({ region: '', day: '2025-08-13', avg_temp_c: 32.5, max_temp_c: 38.0, source_note: '' })
const holidayForm = ref({ day: '2025-08-19', name: '' })
const outageForm = ref({ customer_id: 1, day: '2025-08-14', kind: 'curtailment', note: '' })

const period = computed(() => periodStore.period)
const regions = computed(() => [...new Set(customers.value.map((c) => c.region).filter(Boolean))])

async function loadAll() {
  loading.value = true
  error.value = ''
  try {
    const [b, r, c, ct, ev, mt, w, h, o] = await Promise.all([
      api('/batches', { params: { limit: 80 } }),
      api('/rules'),
      api('/customers'),
      api('/contracts'),
      api('/events', { params: { period_month: period.value } }),
      api('/meters'),
      api('/weather/daily', { params: { limit: 120 } }),
      api('/holidays'),
      api('/outages'),
    ])
    batches.value = b
    rules.value = r
    customers.value = c
    contracts.value = ct
    events.value = ev
    meters.value = mt
    weather.value = w
    holidays.value = h
    outages.value = o
    if (!batchForm.value.meter_no && mt.length) batchForm.value.meter_no = mt[0].meter_no
    if (!batchForm.value.day && ev.length) batchForm.value.day = ev[0].event_date
    if (ct.length && !contractForm.value.customer_id) contractForm.value.customer_id = ct[0].customer_id
    if (!weatherForm.value.region && regions.value.length) weatherForm.value.region = regions.value[0]
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function buildIntervals(day, event, low, high) {
  const start = new Date(`${day}T00:00:00Z`)
  const rows = []
  for (let i = 0; i < 96; i += 1) {
    const ts = new Date(start.getTime() + i * 15 * 60000)
    const hh = ts.getUTCHours()
    const mm = ts.getUTCMinutes()
    const clock = `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`
    const inWindow = clock >= event.start_time && clock < event.end_time
    rows.push({ ts: ts.toISOString(), kwh: inWindow ? low : high })
  }
  return rows
}

async function submitBatch() {
  const event = events.value.find((e) => e.event_date === batchForm.value.day) || events.value[0]
  if (!event) {
    result.value = { ok: false, message: '请先选择有活动定义的日期' }
    return
  }
  submitting.value = true
  result.value = null
  impact.value = null
  try {
    const preview = await api('/batches/preview-impact', {
      method: 'POST',
      body: { batch_key: 'preview', meter_no: batchForm.value.meter_no, intervals: buildIntervals(batchForm.value.day, event, 0, 1) },
    })
    impact.value = preview
  } catch (e) {
    impact.value = null
  }
  try {
    const body = {
      batch_key: `LATE-${batchForm.value.meter_no}-${batchForm.value.day}-${Date.now()}`,
      meter_no: batchForm.value.meter_no,
      intervals: buildIntervals(batchForm.value.day, event, batchForm.value.low, batchForm.value.high),
      source_note: batchForm.value.note,
    }
    const data = await api('/batches', { method: 'POST', body })
    result.value = { ok: true, message: `已登记批次 #${data.id}（v${data.version}），影响面可在「差异与追溯」中按批次发起定向重算。` }
    await loadAll()
  } catch (e) {
    result.value = { ok: false, message: e.message }
  } finally {
    submitting.value = false
  }
}

async function submitRule() {
  submitting.value = true
  result.value = null
  try {
    const data = await api('/rules', { method: 'POST', body: ruleForm.value })
    result.value = { ok: true, message: `已登记规则 ${data.rule_code} v${data.version}` }
    await loadAll()
  } catch (e) {
    result.value = { ok: false, message: e.message }
  } finally {
    submitting.value = false
  }
}

async function validateRule() {
  const event = events.value[0]
  if (!event) return
  try {
    ruleValidation.value = await api('/rules/validate', {
      method: 'POST',
      body: { event_id: event.id, lookback_days: ruleForm.value.lookback_days, min_sample_days: ruleForm.value.min_sample_days },
    })
  } catch (e) {
    ruleValidation.value = null
    result.value = { ok: false, message: e.message }
  }
}

async function submitContract() {
  submitting.value = true
  result.value = null
  try {
    const data = await api('/contracts', { method: 'POST', body: contractForm.value })
    result.value = { ok: true, message: `客户 #${data.customer_id} 已登记签约 v${data.version}` }
    await loadAll()
  } catch (e) {
    result.value = { ok: false, message: e.message }
  } finally {
    submitting.value = false
  }
}

async function submitWeather() {
  submitting.value = true
  result.value = null
  try {
    const data = await api('/weather/daily', { method: 'POST', body: weatherForm.value })
    result.value = { ok: true, message: `已登记 ${data.region} ${data.day} 气象日值（v${data.version}）` }
    await loadAll()
  } catch (e) {
    result.value = { ok: false, message: e.message }
  } finally {
    submitting.value = false
  }
}

async function submitHoliday() {
  submitting.value = true
  result.value = null
  try {
    const data = await api('/holidays', { method: 'POST', body: holidayForm.value })
    result.value = { ok: true, message: `已登记节假日 ${data.day}（${data.name}），该日将从基线样本与伪活动日排除` }
    await loadAll()
  } catch (e) {
    result.value = { ok: false, message: e.message }
  } finally {
    submitting.value = false
  }
}

async function submitOutage() {
  submitting.value = true
  result.value = null
  try {
    const data = await api('/outages', { method: 'POST', body: outageForm.value })
    result.value = { ok: true, message: `已登记客户 #${data.customer_id} ${data.day} ${data.kind === 'outage' ? '停电' : '限电'}记录` }
    await loadAll()
  } catch (e) {
    result.value = { ok: false, message: e.message }
  } finally {
    submitting.value = false
  }
}

onMounted(loadAll)
</script>
