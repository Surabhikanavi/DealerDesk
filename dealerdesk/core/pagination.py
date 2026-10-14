from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    """{"count", "next", "previous", "results"}; ?page= (1-based), ?page_size= (default 20, max 100)."""

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


def paginate(request, queryset, view, serialize):
    paginator = StandardPagination()
    page = paginator.paginate_queryset(queryset, request, view=view)
    return paginator.get_paginated_response([serialize(obj) for obj in page])
