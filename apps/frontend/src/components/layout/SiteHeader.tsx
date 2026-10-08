import logoUrl from '@/assets/brand/logo.png'
import sponsorsUrl from '@/assets/brand/sponsors.png'

/**
 * 顶部品牌栏（移植自旧「全国水文模拟系统」前端）：
 * - 左：圆形徽标 + 中文主标题 + 英文副标题；
 * - 右：合作单位 logo 条（已去除最右侧山脉科技）。
 * 深蓝渐变沿用主题 primary-900/700，不新造颜色。
 */
export function SiteHeader() {
  return (
    <header className="flex h-[84px] shrink-0 items-center justify-between gap-4 bg-gradient-to-r from-primary-900 via-primary-800 to-primary-700 px-5 shadow-md mobile:h-12">
      <div className="flex items-center gap-3 mobile:min-w-0">
        <img
          src={logoUrl}
          alt="全国水文模拟系统徽标"
          className="h-12 w-12 rounded-full mobile:h-8 mobile:w-8"
          draggable={false}
        />
        <div className="leading-tight mobile:min-w-0">
          <div className="text-[28px] font-extrabold tracking-wide text-white mobile:truncate mobile:text-[16px]">
            全国水文模拟系统（V2.0）
          </div>
          <div className="text-[11px] uppercase tracking-[0.25em] text-primary-100/80 mobile:hidden">
            National Water Modeling
          </div>
        </div>
      </div>
      <img
        src={sponsorsUrl}
        alt="合作单位"
        className="hidden h-14 object-contain lg:block mobile:h-8"
        draggable={false}
      />
    </header>
  )
}
