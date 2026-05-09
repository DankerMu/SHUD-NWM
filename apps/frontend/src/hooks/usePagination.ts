import { useMemo, useState } from 'react'

interface UsePaginationOptions {
  initialPage?: number
  initialPageSize?: number
  initialTotal?: number
}

export function usePagination(options: UsePaginationOptions = {}) {
  const [page, setPage] = useState(options.initialPage ?? 1)
  const [pageSize, setPageSize] = useState(options.initialPageSize ?? 20)
  const [total, setTotal] = useState(options.initialTotal ?? 0)

  return useMemo(
    () => ({
      page,
      pageSize,
      total,
      offset: (page - 1) * pageSize,
      pageCount: Math.max(1, Math.ceil(total / pageSize)),
      setPage,
      setPageSize,
      setTotal,
    }),
    [page, pageSize, total],
  )
}
