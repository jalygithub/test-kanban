import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 后端跑在 8000，开发期由 Vite 代理，避免跨域
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 监听所有网卡：电脑连手机热点时，手机要用电脑的局域网 IP 打开页面
    // （`http://192.168.137.x:5173`）。只听 loopback 的话手机根本连不上，
    // 于是邀请链接 / 收银台在手机上永远打不开 —— 而这两个页面就是给人用手机看的。
    // ⚠️ 代价：同网络下的设备都能访问本服务，后端 `/api/system/stop` 目前无鉴权。
    host: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
