<template>
  <div class="panel">
    <div class="panel-head">
      <h2>总览</h2>
      <span class="small muted">{{ period }}</span>
      <div class="grow" style="flex:1"></div>
      <button class="btn small" @click="load" :disabled="loading">{{ loading ? '加载中…' : '刷新' }}</button>
    </div>
    <div v-if="error" class="panel-body"><div class="notice danger">{{ error }}</div></div>
    <div v-else class="panel-body">
      <div class="grid cols-4">
        <div class="panel stat">
          <div class="label">已发布账本合计</div>
          <div class="value">{{ fmt.money(data.ledger_published_amount) }}</div>
          <div class="hint">{{ data.ledger_published_count }} 条现行账本（不含已被替换版本）</div>
        </div>
        <div class="panel stat">
          <div class="label">待审批调整差额</div>
          <div class="value" :style="{ color: data.adjustment_pending_delta < 0 ? 'var(--danger)' : 'var(--text)' }">
            {{ fmt.money(data.adjustment_pending_delta) }}
          </div>
          <div class="hint">{{ data.adjustment_pending_count }} 笔待批；已批准 {{ fmt.money(data.adjustment_approved_delta) }}</div>
        </div>
        <div class="panel stat">
          <div class="label">运行总数</div>
          <div class="value">{{ data.run_total }}</div>
          <div class="hint">待核 {{ data.run_needs_review }} · 失败 {{ data.run_failed }}</div>
        </div>
        <div class="panel stat">
          <div class="label">计算图节点</div>
          <div class="value">{{ totalNodes }}</div>
          <div class="hint">补数 → 基线 → 响应电量 → 结算，四节点/窗口</div>
        </div>
      </div>

      <div class="grid cols-2" style="margin-top:14px">
        <section class="panel">
          <div class="panel-head"><h2>按结算期发布金额</h2><div style="flex:1"></div><span class="small muted">仅现行版本</span></div>
          <div class="panel-body">
            <div v-if="!data.ledger_by_period?.length" class="empty">暂无已发布账本</div>
            <div v-else class="chart">
              <div v-for="row in data.ledger_by_period" :key="row.period_month" class="col">
                <div class="stack" :style="{ height: barHeight(row.amount) + 'px', background: '#1e5aa8' }">
                  <span :style="{ height: '100%', background: '#1e5aa8' }"></span>
                </div>
                <div class="cap">{{ row.period_month }}</div>
                <div class="num small">{{ fmt.money(row.amount) }}</div>
              </div>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head"><h2>重算策略效率</h2><div style="flex:1"></div><span class="small muted">计算窗口 / 复用窗口</span></div>
          <div class="panel-body">
            <div v-if="!data.strategy_stats?.length" class="empty">暂无运行数据</div>
            <div v-for="s in data.strategy_stats" :key="s.strategy" style="margin-bottom:14px">
              <div style="display:flex;justify-content:space-between;font-size:13px">
                <strong>{{ STRATEGY_LABEL[s.strategy] || s.strategy }}</strong>
                <span class="small muted">{{ s.runs }} 次 · 平均 {{ (s.avg_duration_ms / 1000).toFixed(2) }} s</span>
              </div>
              <div class="bar" style="margin-top:5px">
                <span :style="{ width: share(s.computed_windows), background: '#a15c00' }"></span>
                <span :style="{ width: share(s.reused_windows), background: '#0f7b57' }"></span>
              </div>
              <div class="legend">
                <span><i style="background:#a15c00"></i>实际重算 {{ s.computed_windows }} 窗口</span>
                <span><i style="background:#0f7b57"></i>复用 {{ s.reused_windows }} 窗口</span>
              </div>
            </div>
            <p class="small muted" style="margin-top:10px">
              定向重算只重算受影响窗口，其余窗口复用最近一次成功运行的结果，因此算力更省；
              整期重跑对所有窗口重新计算，便于一致性核对但等待更长。
            </p>
          </div>
        </section>
      </div>

      <section class="panel" style="margin-top:14px">
        <div class="panel-head">
          <h2>最近运行</h2><div style="flex:1"></div>
          <router-link class="small" to="/runs">全部运行 →</router-link>
        </div>
        <table>
          <thead>
            <tr><th>运行</th><th>策略</th><th>状态</th><th class="num">窗口（重算/复用）</th><th>输入截止</th><th>发布</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="r in data.recent_runs" :key="r.id" class="clickable" @click="$router.push(`/runs/${r.id}`)">
              <td class="mono">{{ r.run_code }}</td>
              <td>{{ STRATEGY_LABEL[r.strategy] }}</td>
              <td><StatusBadge :status="r.status" /></td>
              <td class="num">{{ r.target_windows }}（{{ r.computed_windows }}/{{ r.reused_windows }}）</td>
              <td class="small">{{ fmt.dt(r.cutoff_at) }}</td>
              <td><span v-if="r.published_at" class="badge info">已发布</span><span v-else class="small muted">未发布</span></td>
              <td><router-link :to="`/runs/${r.id}`">详情</router-link></td>
            </tr>
          </tbody>
        </table>
        <div v-if="!data.recent_runs?.length" class="empty">暂无运行，请到「运行看板」发起一次运行</div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, fmt, STRATEGY_LABEL } from '../api'
import { periodStore } from '../periodStore'
import StatusBadge from '../components/StatusBadge.vue'

const data = ref({})
const loading = ref(false)
const error = ref('')
const period = computed(() => periodStore.period)

const totalNodes = computed(() => (data.value.recent_runs || []).reduce((a, r) => a + (r.node_count || 0), 0))

function share(v) {
  const computedTotal = (data.value.strategy_stats || []).reduce((a, s) => a + s.computed_windows, 0)
  const total = Math.max(1, computedTotal)
  return `${((v || 0) / total) * 100}%`
}

function barHeight(amount) {
  const max = Math.max(...(data.value.ledger_by_period || []).map((r) => r.amount), 1)
  return Math.max(10, Math.round((amount / max) * 130))
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api('/overview', { params: { period_month: period.value } })
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
