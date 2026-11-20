<template>
  <div>
    <section class="panel">
      <div class="panel-head">
        <h2>差异与追溯</h2><span class="small muted">按来源定位影响窗口，发起定向重算</span>
        <div style="flex:1"></div>
        <button class="btn small" @click="loadRuns">刷新运行</button>
      </div>
      <div class="panel-body">
        <div class="tabs">
          <button :class="{ active: tab === 'compare' }" @click="tab = 'compare'">运行对比</button>
          <button :class="{ active: tab === 'impact' }" @click="tab = 'impact'">来源影响面</button>
          <button :class="{ active: tab === 'trace' }" @click="tab = 'trace'">调整追溯</button>
        </div>

        <!-- compare -->
        <div v-if="tab === 'compare'">
          <div class="toolbar">
            <div class="field"><label>左（基准）运行</label><select v-model="leftId"><option :value="null">选择运行</option><option v-for="r in runs" :key="r.id" :value="r.id">{{ r.run_code }} · v{{ r.version }} · {{ STRATEGY_LABEL[r.strategy] }}</option></select></div>
            <div class="field"><label>右（对比）运行</label><select v-model="rightId"><option :value="null">选择运行</option><option v-for="r in runs" :key="r.id" :value="r.id">{{ r.run_code }} · v{{ r.version }} · {{ STRATEGY_LABEL[r.strategy] }}</option></select></div>
            <button class="btn primary" :disabled="!leftId || !rightId || leftId === rightId" @click="compareRuns">对比</button>
          </div>
          <div v-if="compare" style="margin-top:14px">
            <div class="grid cols-3">
              <div class="panel stat"><div class="label">变化窗口</div><div class="value">{{ compare.changed_count }}</div></div>
              <div class="panel stat"><div class="label">金额合计差额</div><div class="value" :style="{ color: compare.total_delta < 0 ? 'var(--danger)' : 'var(--ok)' }">{{ fmt.money(compare.total_delta) }}</div></div>
              <div class="panel stat"><div class="label">比较运行</div><div class="value" style="font-size:15px">{{ compare.left.run_code }} → {{ compare.right.run_code }}</div><div class="hint">截止 {{ fmt.dt(compare.left.cutoff_at) }} / {{ fmt.dt(compare.right.cutoff_at) }}</div></div>
            </div>
            <table style="margin-top:14px">
              <thead><tr><th>客户</th><th>活动</th><th class="num">左金额</th><th class="num">右金额</th><th class="num">差额</th><th>变化</th><th>右状态</th></tr></thead>
              <tbody>
                <tr v-for="row in compare.rows" :key="row.customer_id + '-' + row.event_id" :style="row.changed ? 'background:#fffdf5' : ''">
                  <td>{{ row.customer_code }}</td>
                  <td class="small">{{ row.event_code }}<div class="muted">{{ row.event_date }}</div></td>
                  <td class="num">{{ row.left?.amount != null ? fmt.money(row.left.amount) : '—' }}</td>
                  <td class="num">{{ row.right?.amount != null ? fmt.money(row.right.amount) : '—' }}</td>
                  <td class="num" :style="{ color: row.delta_amount < 0 ? 'var(--danger)' : (row.delta_amount > 0 ? 'var(--ok)' : 'inherit') }">{{ row.delta_amount != null ? fmt.money(row.delta_amount) : '—' }}</td>
                  <td><span v-if="row.changed" class="badge warn">已变化</span><span v-else class="small muted">一致</span></td>
                  <td><StatusBadge :status="row.right?.status" /></td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- impact -->
        <div v-if="tab === 'impact'">
          <div class="notice info small" style="margin-bottom:12px">
            输入批次、签约或规则版本 ID。系统先查血缘（已参与的节点），再按覆盖区间/生效日推断新来源影响面，避免新补传批次漏算。
          </div>
          <div class="toolbar">
            <div class="field"><label>批次 ID</label><input v-model="impactForm.batch_id" placeholder="如 233" style="width:120px" /></div>
            <div class="field"><label>签约 ID</label><input v-model="impactForm.contract_id" placeholder="可空" style="width:120px" /></div>
            <div class="field"><label>规则 ID</label><input v-model="impactForm.rule_id" placeholder="可空" style="width:120px" /></div>
            <button class="btn primary" @click="analyzeImpact">分析影响面</button>
          </div>
          <div v-if="impact" style="margin-top:14px">
            <div class="notice" :class="impact.affected_window_count ? 'warn' : 'ok'">
              受影响窗口 {{ impact.affected_window_count }} 个
            </div>
            <table style="margin-top:10px">
              <thead><tr><th>活动</th><th>客户</th><th>最新运行</th><th>已发布</th></tr></thead>
              <tbody>
                <tr v-for="w in impact.windows" :key="w.event_id + '-' + w.customer_id">
                  <td class="small">{{ w.event_code }}<div class="muted">{{ w.event_date }}</div></td>
                  <td>{{ w.customer_code }}</td>
                  <td><router-link :to="`/runs/${w.latest_run_id}`" v-if="w.latest_run_id">{{ w.latest_run_code }}</router-link><span v-else class="muted">—</span></td>
                  <td><span class="badge" :class="w.published ? 'info' : 'gray'">{{ w.published ? '已发布' : '未发布' }}</span></td>
                </tr>
              </tbody>
            </table>
            <div style="margin-top:12px">
              <button class="btn primary" :disabled="!canPlan() || !impact.affected_window_count || promoting" @click="runTargeted">
                {{ promoting ? '执行中…' : '发起定向重算' }}
              </button>
              <span class="small muted" style="margin-left:10px">只重算受影响窗口，其余窗口复用最近成功结果</span>
            </div>
            <details style="margin-top:12px">
              <summary class="small muted" style="cursor:pointer">影响面证据（{{ impact.evidence.length }} 条）</summary>
              <pre class="json" style="margin-top:8px">{{ JSON.stringify(impact.evidence, null, 2) }}</pre>
            </details>
          </div>
          <div v-if="impactMessage" class="notice" :class="impactMessage.ok ? 'ok' : 'danger'" style="margin-top:12px">{{ impactMessage.text }}</div>
        </div>

        <!-- trace -->
        <div v-if="tab === 'trace'">
          <div class="toolbar">
            <div class="field"><label>调整记录</label>
              <select v-model="adjustmentId">
                <option :value="null">选择调整</option>
                <option v-for="a in adjustments" :key="a.id" :value="a.id">{{ a.code }} · {{ a.customer_code }} · {{ fmt.money(a.delta_amount) }}</option>
              </select>
            </div>
            <button class="btn primary" :disabled="!adjustmentId" @click="loadTrace">追溯来源</button>
          </div>
          <div v-if="trace" style="margin-top:14px">
            <div class="grid cols-2">
              <section class="panel">
                <div class="panel-head"><h3>调整</h3></div>
                <div class="panel-body">
                  <dl class="kv">
                    <dt>调整编号</dt><dd class="mono">{{ trace.adjustment.code }}</dd>
                    <dt>客户 / 活动</dt><dd>{{ trace.adjustment.customer_code }} · {{ trace.adjustment.event_code }}</dd>
                    <dt>差额</dt><dd>{{ fmt.money(trace.adjustment.delta_amount) }}（{{ trace.adjustment.delta_kwh }} kWh）</dd>
                    <dt>原因</dt><dd>{{ trace.adjustment.reason }}</dd>
                    <dt>状态</dt><dd><StatusBadge :status="trace.adjustment.status" /></dd>
                    <dt>运行</dt><dd><router-link :to="`/runs/${trace.run?.id}`">{{ trace.run?.run_code }}</router-link></dd>
                    <dt>输入截止</dt><dd>{{ fmt.dt(trace.cutoff_at) }}</dd>
                  </dl>
                </div>
              </section>
              <section class="panel">
                <div class="panel-head"><h3>凭证链</h3></div>
                <div class="panel-body">
                  <div v-for="v in trace.vouchers" :key="v.id" class="notice info small" style="margin-bottom:8px">
                    <strong>{{ v.voucher_no }}</strong> · {{ v.kind === 'statement' ? '结算凭证' : '调整凭证' }} · {{ fmt.money(v.amount) }}
                    <div class="mono small" style="margin-top:4px">{{ v.reference }}</div>
                  </div>
                  <div v-if="!trace.vouchers.length" class="empty" style="padding:8px">尚无凭证（原账本未付款）</div>
                </div>
              </section>
            </div>
            <section class="panel" style="margin-top:14px">
              <div class="panel-head"><h3>参与计算的来源</h3><div style="flex:1"></div><span class="small muted">{{ trace.sources.length }} 条</span></div>
              <table>
                <thead><tr><th>节点</th><th>来源类型</th><th>来源 ID</th><th>详情</th></tr></thead>
                <tbody>
                  <tr v-for="(s, i) in trace.sources" :key="i">
                    <td>{{ NODE_LABEL[s.node_type] || s.node_type }}</td>
                    <td>{{ { batch: '批次', rule: '规则', contract: '签约', event: '活动', node: '节点' }[s.source_type] || s.source_type }}</td>
                    <td class="mono">#{{ s.ref_id }}</td>
                    <td class="small mono">{{ JSON.stringify(s.meta) }}</td>
                  </tr>
                </tbody>
              </table>
              <div v-if="!trace.sources.length" class="empty">无来源</div>
            </section>
            <section class="panel" style="margin-top:14px">
              <div class="panel-head">
                <h3>输入快照</h3><div style="flex:1"></div>
                <span class="small muted">批次 {{ trace.snapshot.batch_snapshot?.length || 0 }} 个 · 规则 {{ Object.keys(trace.snapshot.rule_snapshot || {}).length }} 版 · 签约 {{ Object.keys(trace.snapshot.contract_snapshot || {}).length }} 版</span>
              </div>
              <div class="panel-body">
                <div class="small"><strong>批次：</strong><span class="mono">{{ compactIds(trace.snapshot.batch_snapshot) }}</span></div>
                <div class="small" style="margin-top:6px"><strong>规则：</strong>
                  <span v-for="(v, k) in trace.snapshot.rule_snapshot" :key="k" class="badge gray" style="margin-right:4px">{{ v.rule_code }} v{{ v.version }}</span>
                </div>
                <div class="small" style="margin-top:6px"><strong>签约：</strong>
                  <span v-for="(v, k) in trace.snapshot.contract_snapshot" :key="k" class="badge gray" style="margin-right:4px">客户 #{{ v.customer_id }} v{{ v.version }}</span>
                </div>
              </div>
            </section>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api, canPlan, fmt, STRATEGY_LABEL, NODE_LABEL } from '../api'
import { periodStore } from '../periodStore'
import StatusBadge from '../components/StatusBadge.vue'

const route = useRoute()
const tab = ref('compare')
const runs = ref([])
const adjustments = ref([])
const leftId = ref(null)
const rightId = ref(null)
const compare = ref(null)
const impactForm = ref({ batch_id: '', contract_id: '', rule_id: '' })
const impact = ref(null)
const impactMessage = ref(null)
const promoting = ref(false)
const adjustmentId = ref(null)
const trace = ref(null)

async function loadRuns() {
  runs.value = await api('/runs', { params: { period_month: periodStore.period } })
  if (runs.value.length >= 2) {
    // Default: oldest run vs newest, so a revision shows up immediately.
    if (!leftId.value) leftId.value = runs.value[runs.value.length - 1].id
    if (!rightId.value) rightId.value = runs.value[0].id
  }
}

async function loadAdjustments() {
  adjustments.value = await api('/adjustments', { params: { period_month: periodStore.period } })
}

async function compareRuns() {
  compare.value = await api('/compare', { params: { left_run_id: leftId.value, right_run_id: rightId.value } })
}

async function analyzeImpact() {
  impactMessage.value = null
  const params = {}
  if (impactForm.value.batch_id) params.batch_id = [Number(impactForm.value.batch_id)]
  if (impactForm.value.contract_id) params.contract_id = [Number(impactForm.value.contract_id)]
  if (impactForm.value.rule_id) params.rule_id = [Number(impactForm.value.rule_id)]
  try {
    impact.value = await api('/impact', { params })
  } catch (e) {
    impact.value = null
    impactMessage.value = { ok: false, text: e.message }
  }
}

async function runTargeted() {
  promoting.value = true
  impactMessage.value = null
  const body = { period_month: periodStore.period, strategy: 'targeted', notes: '影响面定向重算' }
  if (impactForm.value.batch_id) body.batch_ids = [Number(impactForm.value.batch_id)]
  if (impactForm.value.contract_id) body.contract_ids = [Number(impactForm.value.contract_id)]
  if (impactForm.value.rule_id) body.rule_ids = [Number(impactForm.value.rule_id)]
  try {
    const run = await api('/runs', { method: 'POST', body })
    impactMessage.value = {
      ok: true,
      text: `定向重算 ${run.run_code} 完成：重算 ${run.computed_windows} / 复用 ${run.reused_windows} 窗口，状态 ${run.status}。`,
    }
    await loadRuns()
  } catch (e) {
    impactMessage.value = { ok: false, text: e.message }
  } finally {
    promoting.value = false
  }
}

function compactIds(ids) {
  const list = ids || []
  if (!list.length) return '—'
  if (list.length <= 24) return list.join(', ')
  return `${list.slice(0, 24).join(', ')} …（共 ${list.length} 个）`
}

async function loadTrace() {
  trace.value = null
  try {
    trace.value = await api(`/adjustments/${adjustmentId.value}/trace`)
  } catch (e) {
    impactMessage.value = { ok: false, text: e.message }
  }
}

onMounted(async () => {
  await Promise.all([loadRuns(), loadAdjustments()])
  if (route.query.right) rightId.value = Number(route.query.right)
  if (route.query.adjustment) {
    tab.value = 'trace'
    adjustmentId.value = Number(route.query.adjustment)
    await loadTrace()
  }
  if (leftId.value && rightId.value && leftId.value !== rightId.value) await compareRuns()
})
</script>
