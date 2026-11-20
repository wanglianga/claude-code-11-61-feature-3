<template>
  <div class="login-wrap">
    <div class="login-card">
      <h1>需求响应结算重算编排平台</h1>
      <p class="sub">省级负荷聚合商 · 客户级需求响应结算</p>
      <form @submit.prevent="submit">
        <div class="field">
          <label>用户名</label>
          <input v-model="username" autocomplete="username" placeholder="planner" />
        </div>
        <div class="field">
          <label>密码</label>
          <input v-model="password" type="password" autocomplete="current-password" placeholder="••••••••" />
        </div>
        <div v-if="error" class="notice danger">{{ error }}</div>
        <button class="btn primary" type="submit" :disabled="loading">{{ loading ? '登录中…' : '登录' }}</button>
      </form>
      <div class="demo-users">
        演示账号：
        <div>
          <button v-for="u in demoUsers" :key="u.username" class="btn small" type="button" @click="fill(u)">
            {{ u.label }}（{{ u.username }}）
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, setSession } from '../api'

const router = useRouter()
const route = useRoute()
const username = ref('planner')
const password = ref('planner123')
const loading = ref(false)
const error = ref('')

const demoUsers = [
  { username: 'planner', password: 'planner123', label: '计划员' },
  { username: 'approver', password: 'approver123', label: '审批员' },
  { username: 'viewer', password: 'viewer123', label: '只读' },
]

function fill(u) {
  username.value = u.username
  password.value = u.password
}

async function submit() {
  loading.value = true
  error.value = ''
  try {
    const data = await api('/auth/login', { method: 'POST', body: { username: username.value, password: password.value } })
    setSession(data.access_token, data.user)
    router.push(route.query.redirect || '/')
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
</script>
