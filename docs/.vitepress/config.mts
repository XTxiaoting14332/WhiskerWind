import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'zh-CN',
  title: 'WhiskerWind',
  description: '基于官方 Linux QQ 客户端的 Bot 协议端',
  cleanUrls: true,

  vite: {
    publicDir: '../img',
  },

  markdown: {
    container: {
      warningLabel: 'Warning',
    },
  },

  head: [
    ['link', { rel: 'icon', type: 'image/png', sizes: '32x32', href: '/icons/whiskerwind-32.png' }],
    ['link', { rel: 'apple-touch-icon', sizes: '180x180', href: '/icons/whiskerwind-180.png' }],
  ],

  themeConfig: {
    logo: '/icons/whiskerwind-128.png',
    siteTitle: 'WhiskerWind',

    nav: [
      { text: '指南', link: '/guide/getting-started' },
      { text: '接入框架', link: '/guide/bot-frameworks' },
      { text: '参考', link: '/architecture' },
    ],

    sidebar: [
      {
        text: '入门指南',
        items: [
          { text: '快速开始', link: '/guide/getting-started' },
          { text: '配置参数', link: '/guide/configuration' },
          { text: '接入 Bot 框架', link: '/guide/bot-frameworks' },
          { text: '运行与维护', link: '/guide/operations' },
          { text: '已知问题与排查', link: '/guide/known-issues' },
          { text: '本地开发', link: '/guide/development' },
        ],
      },
      {
        text: '参考文档',
        items: [
          { text: '实现原理', link: '/architecture' },
          { text: '功能差异', link: '/compatibility' },
          { text: 'WebUI 接口', link: '/webui-api' },
          { text: 'automatic 协议', link: '/automatic-protocol' },
          { text: 'WebUI 设计', link: '/webui-design' },
        ],
      },
    ],

    socialLinks: [{ icon: 'github', link: 'https://github.com/XTxiaoting14332/WhiskerWind' }],

    outline: { level: [2, 3], label: '本页目录' },
    docFooter: { prev: '上一页', next: '下一页' },
    returnToTopLabel: '返回顶部',
    sidebarMenuLabel: '菜单',
    darkModeSwitchLabel: '外观',
    lightModeSwitchTitle: '切换到浅色模式',
    darkModeSwitchTitle: '切换到深色模式',
    skipToContentLabel: '跳到内容',

    search: {
      provider: 'local',
      options: {
        translations: {
          button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
          modal: {
            noResultsText: '无法找到相关结果',
            resetButtonTitle: '清除查询条件',
            footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' },
          },
        },
      },
    },

    footer: {
      message: '本项目仅用于概念验证（PoC）与技术研究，与腾讯公司无关。以 GPL-3.0 协议开源。',
    },
  },
})
