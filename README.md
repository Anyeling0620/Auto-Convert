# 🛒 Lenovo Shop - 联想官方商城复刻

<div align="center">

<img src="https://img.shields.io/badge/Lenovo-E2231A?style=for-the-badge&logo=lenovo&logoColor=white" alt="Lenovo Badge" />
<img src="https://img.shields.io/badge/React_19-20232A?style=for-the-badge&logo=react&logoColor=61DAFB" alt="React" />
<img src="https://img.shields.io/badge/TypeScript-007ACC?style=for-the-badge&logo=typescript&logoColor=white" alt="TypeScript" />
<img src="https://img.shields.io/badge/Vite_7-646CFF?style=for-the-badge&logo=vite&logoColor=white" alt="Vite" />
<img src="https://img.shields.io/badge/Tailwind_CSS-38B2AC?style=for-the-badge&logo=tailwind-css&logoColor=white" alt="TailwindCSS" />

<br/>

**—— 打造全场景、高性能的现代化电商购物体验 ——**

[⚡ 快速开始](#-快速开始) • [📱 界面预览](#-界面预览) • [🛠️ 技术栈](#-技术栈) • [📂 文件结构](#-文件结构) • [📝 TODO清单](#-todo-清单)

</div>

---

## 📖 项目概述

**Lenovo Shop** 是一个高仿联想官方商城的全栈电商平台解决方案。项目深度还原了真实的电商业务场景，采用最新的 React 19 生态系统，旨在探索极致的性能优化与用户体验设计。

### ✨ 核心愿景
| 维度 | 描述 |
| :--- | :--- |
| 🚀 **极致性能** | 基于 **Vite 7** 构建，秒级启动，配合 React 19 Concurrent 特性，操作丝般顺滑。 |
| 📱 **全端适配** | 响应式设计 + **Capacitor** 跨平台支持，一套代码完美运行于 Web、iOS 和 Android。 |
| 🎨 **优雅设计** | 遵循联想品牌视觉规范，利用 **TailwindCSS** 打造像素级还原的现代化 UI。 |
| 🔒 **安全体系** | 完整的用户认证流程（JWT）、数据加密传输及 CSRF 防护机制。 |

---

## 📸 界面预览

| 🏠 首页展示 | 🛍️ 商品详情 |
| :---: | :---: |
| ![首页](https://via.placeholder.com/375x667/E1140A/FFFFFF?text=Home+Page) | ![详情页](https://via.placeholder.com/375x667/0066CC/FFFFFF?text=Product+Detail) |

| 🛒 购物车 | 👤 用户中心 |
| :---: | :---: |
| ![购物车](https://via.placeholder.com/375x667/333333/FFFFFF?text=Shopping+Cart) | ![用户中心](https://via.placeholder.com/375x667/F2F2F2/333333?text=User+Profile) |

---

## 🛠️ 技术栈

### 🎨 前端核心
| 技术 | 版本 | 说明 |
| :--- | :--- | :--- |
| ![React](https://img.shields.io/badge/-React-black?logo=react) | `19.2.0` | 最新稳定版，使用 Hooks + Concurrent 特性 |
| ![TypeScript](https://img.shields.io/badge/-TypeScript-black?logo=typescript) | `5.9.3` | 严格模式，提供完整的类型安全支持 |
| ![Vite](https://img.shields.io/badge/-Vite-black?logo=vite) | `7.2.2` | 下一代前端构建工具，极速 HMR |

### 💅 UI & 交互
| 技术 | 版本 | 说明 |
| :--- | :--- | :--- |
| **TailwindCSS** | `3.4.18` | 原子化 CSS 框架，构建设计系统 |
| **Ant Design** | `6.0.0` | 企业级 UI 组件库，用于后台及复杂表单 |
| **Framer Motion** | `12.23.24` | 生产级动画库，负责页面转场与微交互 |
| **Swiper** | `12.0.3` | 强大的触摸滑动插件，用于轮播图 |

### 🔧 状态与数据流
| 技术 | 版本 | 说明 |
| :--- | :--- | :--- |
| **Zustand** | `5.0.9` | 极简主义的状态管理方案 (Auth/User Store) |
| **React Hook Form** | `7.67.0` | 高性能表单验证解决方案 |
| **Axios** | `1.13.2` | 统一的 HTTP 请求拦截与封装 |
| **WebSocket** | `4.13.0` | 实时消息推送与通讯 |

### 📱 移动端与工程化
- **跨平台**: Capacitor (Android/iOS 原生集成)
- **代码规范**: ESLint 9.x + Prettier + PostCSS

---

## 🏗️ 项目架构

### 数据流向图
```mermaid
graph LR
    User(用户交互) --> UI[React 组件]
    UI --> Hooks[Custom Hooks]
    Hooks --> Store[Zustand Store]
    Store --> Service[API Services]
    Service --> Server((后端 API))
    Server -.->|WebSocket| UI
📂 目录结构概览
Plaintext

lenovo-shop/
├── 📁 android/            # Android 原生工程
├── 📁 src/
│   ├── 📁 assets/         # 静态资源 (Mock Data, Images)
│   ├── 📁 component/      # UI 组件库 (按功能模块划分)
│   ├── 📁 context/        # React Context (全局配置)
│   ├── 📁 hooks/          # 自定义 Hooks (逻辑复用)
│   ├── 📁 pages/          # 路由页面组件
│   ├── 📁 services/       # API 服务层 (Axios 封装)
│   ├── 📁 store/          # Zustand 状态管理
│   ├── 📁 types/          # TypeScript 类型定义
│   └── main.tsx           # 入口文件
└── 📄 vite.config.ts      # 构建配置
📂 文件结构详解 (点击展开)
<details> <summary><strong>🔍 点击查看核心模块说明</strong></summary>

1. 核心页面 (src/pages/)
Index.tsx: 商城首页，聚合轮播、推荐、秒杀模块。

ProductDetail.tsx: (核心) 商品详情页，包含规格选择、图片放大、评论分页等复杂逻辑。

ShoppingCart.tsx: 购物车管理，通过 Context 实现状态同步。

UserCenter.tsx: 用户个人中心，包含订单管理与设置。

2. 关键组件 (src/component/)
Auth: AuthForm.tsx (登录注册), VerificationCodeField.tsx (验证码倒计时)。

Product: ProductComments.tsx (评价系统), DeliverySelector.tsx (省市区三级联动)。

Common: Carousel (轮播), Header (全局导航), Layout (布局容器)。

3. 数据与状态
assets/data/mockProducts.ts: 完整的商品 Mock 数据，包含 20+ SKU。

store/authStore.ts: 用户认证状态（Token 管理）。

context/CartContext.tsx: 购物车逻辑核心（增删改查）。

4. 服务层 (src/services/)
AxiosService.ts: 封装拦截器，处理 Token 注入与全局错误提示。

ws/: WebSocket 消息路由与心跳检测机制。

</details>

🎯 功能特性清单
🛒 沉浸式购物体验
✅ 全链路流程：浏览 -> 搜索 -> 详情 -> 购物车 -> 结算 -> 支付 -> 订单。

✅ 智能搜索：支持模糊搜索、多维度筛选（价格/销量/评价）及排序。

✅ 限时秒杀：包含倒计时、库存实时锁定、高并发模拟逻辑。

👤 用户与会员系统
✅ 多模态登录：支持手机号验证码登录、邮箱密码登录。

✅ 个人中心：收货地址管理（三级联动）、收藏夹、浏览历史。

✅ 评价互动：支持带图评价、评分统计、标签筛选。

💻 极致工程化
✅ 性能优化：路由懒加载、图片预加载、骨架屏 (Skeleton) 等待体验。

✅ 移动端原生化：集成 Capacitor，调用设备震动、相机等原生能力。

✅ TypeScript：100% 类型覆盖，确保代码健壮性。

🚀 快速开始
环境准备
Node.js >= 18.0.0

pnpm >= 8.0.0

安装与运行
Bash

# 1. 克隆仓库
git clone [https://github.com/your-org/lenovo-shop.git](https://github.com/your-org/lenovo-shop.git)

# 2. 进入目录
cd lenovo-shop

# 3. 安装依赖 (推荐使用 pnpm)
pnpm install

# 4. 启动开发服务器
pnpm dev
📱 移动端调试 (Android)
Bash

# 同步 Web 代码到原生目录
npx cap sync android

# 打开 Android Studio 进行真机调试
npx cap open android
📋 TODO 清单
🔥 High Priority
[ ] 性能优化: 引入 react-window 实现长列表虚拟滚动。

[ ] 图片优化: 全站图片接入 WebP 格式并实现渐进式加载。

[ ] 国际化: 引入 i18next 支持中/英双语切换。

📈 Medium Priority
[ ] 深色模式: 基于 TailwindCSS 实现 Dark Mode 一键切换。

[ ] 单元测试: 为 useCart 等核心 Hook 增加 Jest 测试用例。

[ ] 物流追踪: 模拟真实的物流时间轴展示。

🤝 贡献指南
我们非常欢迎社区贡献！如果您有好的建议或发现了 Bug：

Fork 本仓库

新建分支 git checkout -b feature/AmazingFeature

提交更改 git commit -m 'feat: Add some AmazingFeature'

推送到分支 git push origin feature/AmazingFeature

提交 Pull Request

<div align="center">

Lenovo Shop Project © 2024. Made with ❤️ by Frontend Team.

⬆️ 返回顶部

</div>
