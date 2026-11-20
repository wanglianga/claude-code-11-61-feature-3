<template>
  <router-view v-if="isPublic" />
  <div v-else class="shell">
    <aside class="side">
      <div class="brand">
        <strong>需求响应结算重算平台</strong>
        <span>负荷聚合商 · 客户级结算</span>
      </div>
      <nav>
        <router-link v-for="item in navItems" :key="item.path" :to="item.path" :class="{ active: isActive(item.path) }">
          <span>{{ item.icon }}</span><span>{{ item.label }}</span>
        </router-link>
      </nav>
      <div class="side-foot">
        演示数据期 2025-08<br />
        <a href="/docs" target="_blank" style="color:#9fc0e8">OpenAPI / Swagger UI ↗</a>
      </div>
    </aside>
    <div class="main">
      <header class="topbar">
        <h1>{{ title }}</h1>
        <div class="grow"></div>
        <span class="small muted">结算期</span>
        <select v-model="selectedPeriod" style="width:140px">
          <option v-for="p in periods" :key="p.period_month" :value="p.period_month">{{ p.period_month }}</option>
        </select>
        <span class="badge info">{{ roleLabel }}</span>
        <span class="small muted">{{ auth.user?.display_name }}</span>
        <button class="btn small" @click="logout">退出</button>
      </header>
      <main class="content">
        <router-view :key="$route.fullPath + selectedPeriod" />
      </main>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, auth, clearSession, ROLE_LABEL } from './api'
import { periodStore } from './periodStore'

const route = useRoute()
const router = useRouter()
const periods = ref([])
const selectedPeriod = ref(periodStore.period)
const navItems = [
  { path: '/', label: '总览', icon: '▦' },
  { path: '/registry', label: '来源登记', icon: '▤' },
  { path: '/runs', label: '运行看板', icon: '⧉' },
  { path: '/uncertainty', label: '基线不确定度', icon: '∿' },
  { path: '/diff', label: '差异与追溯', icon: '⇄' },
  { path: '/ledger', label: '账本与凭证', icon: '▣' },
  { path: '/adjustments', label: '调整台账', icon: '±' },
  { path: '/acceptance', label: '联调验收', icon: '✓' },
]

const isPublic = computed(() => route.meta.public)
const title = computed(() => route.meta.title || '需求响应结算重算平台')
const roleLabel = computed(() => ROLE_LABEL[auth.user?.role] || auth.user?.role || '')

function isActive(path) {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

async function loadPeriods() {
  if (!auth.token) return
  try {
    periods.value = await api('/periods')
    if (periods.value.length) {
      const exists = periods.value.some((p) => p.period_month === selectedPeriod.value)
      if (!exists) selectedPeriod.value = periods.value[0].period_month
      periodStore.period = selectedPeriod.value
    }
  } catch {
    /* token errors handled by api() */
  }
}

watch(selectedPeriod, (v) => {
  periodStore.period = v
})

watch(
  () => [route.path, auth.token],
  () => {
    if (!isPublic.value && periods.value.length === 0) loadPeriods()
    if (selectedPeriod.value) periodStore.period = selectedPeriod.value
  },
)

function logout() {
  clearSession()
  router.push('/login')
}

onMounted(loadPeriods)
</script>
