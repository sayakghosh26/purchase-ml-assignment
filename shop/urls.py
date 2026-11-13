from django.urls import path
from .views import (
    CatalogAPIView, HistoryAPIView, HistoryStatsAPIView,
    OutboxAPIView, PredictionAPIView, PurchaseAPIView,
)

urlpatterns = [
    path("api/purchases/", PurchaseAPIView.as_view(), name="purchase-api"),
    path("api/catalog/<str:sku>/", CatalogAPIView.as_view(), name="catalog-api"),
    path("api/outbox/", OutboxAPIView.as_view(), name="outbox-api"),
    path("api/history/", HistoryAPIView.as_view(), name="history-api"),
    path("api/history/stats/", HistoryStatsAPIView.as_view(), name="history-stats"),
    path("api/predictions/return-risk/", PredictionAPIView.as_view(), name="prediction-api"),
    # Explicit aliases handle non-trailing-slash URLs mentioned in the brief.
    path("api/catalog/<str:sku>", CatalogAPIView.as_view()),
    path("api/outbox", OutboxAPIView.as_view()),
    path("api/history", HistoryAPIView.as_view()),
    path("api/history/stats", HistoryStatsAPIView.as_view()),
    path("api/predictions/return-risk", PredictionAPIView.as_view()),
]
