<template>
  <div>
    <div v-if="error" class="panel panel-body"><div class="notice danger">{{ error }}</div></div>
    <template v-else-if="run">
      <section class="panel">
        <div class="panel-head">
          <h2>{{ run.run_code }}</h2>
          <StatusBadge :status="run.status" />
          <span class="badge gray">{{ STRATEGY_LABEL[run.strategy] }}</span>
          <span v-if="run.published_at" class="badge info">已发布</span>
          <div style="flex:1"></div>
          <button v-if="!run.published_at" class="btn primary small" :disabled="!canApprove() || run.status !== 'success'" @click="publish">
            发布账本
          </button>
          <router-link class="btn small" :to="`/diff?right=${run.id}`">对比差异</router-link>
        </div>
        <div class="panel-body">
          <dl class="kv">
            <dt>结算期 / 版本</dt><dd>{{ run.period_month }} · v{{ run.version }}</dd>
            <dt>输入截止时间</dt><dd>{{ fmt.dt(run.cutoff_at) }}（此后到达的批次与修订不进入本次运行）</dd>
            <dt>窗口（总数/重算/复用）</dt><dd>{{ run.target_windows }} / {{ run.computed_windows }} / {{ run.reused_windows }}</dd>
            <dt>节点数</dt><dd>{{ run.node_count }}</dd>
            <dt>耗时</dt><dd>{{ run.duration_ms }} ms</dd>
            <dt>触发</dt><dd>{{ run.created_by }} · {{ run.trigger_type }}<span v-if="run.trigger_batch_id"> · 批次 #{{ run.trigger_batch_id }}</span></dd>
            <dt>备注</dt><dd>{{ run.notes || '—' }}</dd>
            <dt v-if="run.error">错误</dt><dd v-if="run.error" style="color:var(--danger)">{{ run.error }}</dd>
          </dl>
          <div v-if="message" class="notice" :class="message.ok ? 'ok' : 'danger'" style="margin-top:12px">{{ message.text }}</div>
        </div>
      </section>

      <div class="grid cols-3" style="margin-top:14px">
        <section class="panel">
          <div class="panel-head"><h3>批次快照</h3></div>
          <div class="panel-body">
            <div class="stat" style="padding:0">
              <div class="value">{{ run.batch_snapshot?.length || 0 }}</div>
              <div class="hint">冻结的批次 ID 数；仅这些批次参与计算</div>
            </div>
            <div class="small mono" style="max-height:90px;overflow:auto;margin-top:8px">
              {{ (run.batch_snapshot || []).slice(0, 60).join(', ') }}{{ (run.batch_snapshot || []).length > 60 ? ' …' : '' }}
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head"><h3>规则快照</h3></div>
          <div class="panel-body">
            <div v-if="!Object.keys(run.rule_snapshot || {}).length" class="empty" style="padding:8px">无</div>
            <div v-for="(v, k) in run.rule_snapshot" :key="k" class="small" style="margin-bottom:6px">
              <strong>{{ v.rule_code }} v{{ v.version }}</strong>
              <div class="muted">回看 {{ v.lookback_days }} 天 · 样本≥{{ v.min_sample_days }} · 系数 {{ v.adjustment_factor }}</div>
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head"><h3>签约快照</h3></div>
          <div class="panel-body">
            <div v-if="!Object.keys(run.contract_snapshot || {}).length" class="empty" style="padding:8px">无</div>
            <div v-for="(v, k) in run.contract_snapshot" :key="k" class="small" style="margin-bottom:4px">
              客户 #{{ v.customer_id }} · v{{ v.version }} · {{ v.capacity_kw }} kW
            </div>
          </div>
        </section>
      </div>

      <section class="panel" style="margin-top:14px">
        <div class="panel-head">
          <h2>客户窗口</h2><div style="flex:1"></div>
          <select v-model="windowFilter" style="width:140px">
            <option value="">全部窗口</option>
            <option value="success">成功</option>
            <option value="needs_review">待核</option>
          </select>
        </div>
        <table>
          <thead><tr><th>客户</th><th>活动</th><th>状态</th><th class="num">响应电量</th><th class="num">金额</th><th>不确定度</th><th>待核原因</th><th></th></tr></thead>
          <tbody>
            <tr v-for="w in filteredWindows" :key="w.customer_id + '-' + w.event_id">
              <td><strong>{{ w.customer_code }}</strong><div class="small muted">{{ w.customer_name }}</div></td>
              <td class="small">{{ w.event_code }}<div class="muted">{{ w.event_date }}</div></td>
              <td>
                <StatusBadge :status="w.status" />
                <span v-if="w.reused" class="badge ok" style="margin-left:4px">复用</span>
              </td>
              <td class="num">{{ w.response_kwh ?? '—' }}</td>
              <td class="num">{{ w.amount != null ? fmt.money(w.amount) : '—' }}</td>
              <td>
                <template v-if="w.uncertainty">
                  <span class="badge" :class="UNC_BADGE[w.uncertainty.status] || 'gray'">{{ UNC_LABEL[w.uncertainty.status] || w.uncertainty.status }}</span>
                  <span v-if="w.uncertainty.relative_width != null" class="small muted"> ±{{ (w.uncertainty.relative_width * 100).toFixed(1) }}%</span>
                </template>
                <span v-else class="muted">—</span>
              </td>
              <td class="small" style="color:var(--warn)">{{ w.reason || '' }}</td>
              <td>
                <button class="btn small" @click="openLineage(w)">血缘</button>
                <router-link v-if="w.uncertainty" class="btn small" style="margin-left:4px" to="/uncertainty">区间</router-link>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!filteredWindows.length" class="empty">没有窗口</div>
      </section>

      <section v-if="lineage" class="panel" style="margin-top:14px">
        <div class="panel-head">
          <h2>血缘：{{ lineage.customer?.code }} × {{ lineage.event?.code }}</h2>
          <div style="flex:1"></div>
          <button class="btn small" @click="lineage = null">收起</button>
        </div>
        <div class="panel-body">
          <div class="flow">
            <template v-for="(n, idx) in lineage.nodes" :key="n.id">
              <div class="node" :class="n.status">
                <div style="display:flex;justify-content:space-between;align-items:center">
                  <strong>{{ NODE_LABEL[n.node_type] }}</strong>
                  <StatusBadge :status="n.status" />
                </div>
                <div class="small muted" style="margin:4px 0">节点 #{{ n.id }}<span v-if="n.reused_from_node_id"> · 复用自 #{{ n.reused_from_node_id }}</span></div>
                <div v-if="n.reason" class="small" style="color:var(--warn)">{{ n.reason }}</div>
                <div class="small" style="margin-top:6px">
                  <span v-for="s in n.sources" :key="s.id" class="badge gray" style="margin:2px 3px 0 0">
                    {{ SOURCE_LABEL[s.source_type] }} {{ s.source_type === 'batch' ? `#${s.ref_id} v${s.meta.version}` : (s.source_type === 'rule' ? `v${s.meta.version}` : (s.source_type === 'contract' ? `v${s.meta.version}` : s.ref_id)) }}
                  </span>
                  <span v-if="!n.sources.length" class="muted">无来源</span>
                </div>
                <details v-if="n.output && Object.keys(n.output).length" style="margin-top:6px">
                  <summary class="small muted" style="cursor:pointer">节点输出</summary>
                  <pre class="json" style="margin-top:6px">{{ JSON.stringify(n.output, null, 2) }}</pre>
                </details>
              </div>
              <div v-if="idx < lineage.nodes.length - 1" class="arrow">→</div>
            </template>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api, canApprove, fmt, STRATEGY_LABEL, NODE_LABEL } from '../api'
import StatusBadge from '../components/StatusBadge.vue'

const route = useRoute()
const run = ref(null)
const error = ref('')
const message = ref(null)
const windowFilter = ref('')
const lineage = ref(null)

const SOURCE_LABEL = { batch: '批次', rule: '规则', contract: '签约', event: '活动', node: '节点', weather: '气象', holiday: '节假日', outage: '停/限电' }
const UNC_LABEL = { ok: '充分', degraded: '降级', unavailable: '无法估计' }
const UNC_BADGE = { ok: 'ok', degraded: 'warn', unavailable: 'danger' }

const filteredWindows = computed(() => {
  const windows = run.value?.windows || []
  return windowFilter.value ? windows.filter((w) => w.status === windowFilter.value) : windows
})

async function load() {
  error.value = ''
  try {
    run.value = await api(`/runs/${route.params.id}`)
  } catch (e) {
    error.value = e.message
  }
}

async function openLineage(w) {
  try {
    lineage.value = await api(`/lineage/${run.value.id}/${w.customer_id}/${w.event_id}`)
  } catch (e) {
    message.value = { ok: false, text: e.message }
  }
}

async function publish() {
  try {
    const res = await api(`/runs/${run.value.id}/publish`, { method: 'POST', body: { reason: `发布 ${run.value.run_code}` } })
    message.value = {
      ok: true,
      text: `已发布 ${res.ledger_entry_ids.length} 条账本，生成 ${res.adjustment_ids.length} 笔调整（${fmt.money(res.adjustment_total)}）。`,
    }
    await load()
  } catch (e) {
    message.value = { ok: false, text: e.message }
  }
}

watch(() => route.params.id, load)
onMounted(load)
</script>
