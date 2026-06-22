from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class Persona(StrEnum):
    SHOPPER = "shopper"
    AUTHENTICATED_SHOPPER = "authenticated_shopper"
    ASSOCIATE = "associate"
    MERCHANDISER = "merchandiser"
    EXECUTIVE = "executive"
    CATALOG_ADMIN = "catalog_admin"
    DEVELOPER_TRACE = "developer_trace"
    SEND_CAPABLE = "send_capable"


class Surface(StrEnum):
    REST = "rest"
    CHAT = "chat"
    ADMIN_ASSISTANT = "admin_assistant"
    MCP = "mcp"
    WIDGET = "widget"


class SideEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    SEND = "send"
    ADMIN = "admin"


class Operation(StrEnum):
    CATALOG = "catalog"
    SHOPPER_ACCOUNT = "shopper_account"
    CUSTOMER = "customer"
    COMMUNICATION = "communication"
    MERCHANDISING = "merchandising"
    EXECUTIVE = "executive"
    CATALOG_ADMIN = "catalog_admin"
    OPERATOR = "operator"
    TRACE = "trace"


class ApprovalMode(StrEnum):
    NONE = "none"
    EXPLICIT_BOOLEAN = "explicit_boolean"


REGISTRY_VERSION = "2026-06-22.2"


@dataclass(frozen=True)
class Capability:
    id: str
    name: str
    description: str
    operation: Operation
    side_effect: SideEffect
    allowed_personas: tuple[Persona, ...]
    surfaces: tuple[Surface, ...]
    input_schema: str
    output_schema: str
    service_handler: str
    approval_mode: ApprovalMode = ApprovalMode.NONE
    approval_field: str | None = None
    required_grants: tuple[Persona, ...] = field(default_factory=tuple)
    trace_tags: dict[str, str] = field(default_factory=dict)

    @property
    def requires_approval(self) -> bool:
        return self.approval_mode != ApprovalMode.NONE


def _capability(
    id: str,
    name: str,
    description: str,
    operation: Operation,
    side_effect: SideEffect,
    allowed_personas: Iterable[Persona],
    surfaces: Iterable[Surface],
    input_schema: str,
    output_schema: str,
    service_handler: str,
    *,
    approval_mode: ApprovalMode = ApprovalMode.NONE,
    approval_field: str | None = None,
    required_grants: Iterable[Persona] = (),
) -> Capability:
    return Capability(
        id=id,
        name=name,
        description=description,
        operation=operation,
        side_effect=side_effect,
        allowed_personas=tuple(allowed_personas),
        surfaces=tuple(surfaces),
        input_schema=input_schema,
        output_schema=output_schema,
        service_handler=service_handler,
        approval_mode=approval_mode,
        approval_field=approval_field,
        required_grants=tuple(required_grants),
        trace_tags={
            "capability_id": id,
            "operation": operation.value,
            "side_effect": side_effect.value,
        },
    )


CAPABILITIES: tuple[Capability, ...] = (
    _capability(
        "public.catalog.search",
        "Search published catalog",
        "Search normalized published catalog products.",
        Operation.CATALOG,
        SideEffect.READ,
        (
            Persona.SHOPPER,
            Persona.ASSOCIATE,
            Persona.MERCHANDISER,
            Persona.EXECUTIVE,
            Persona.CATALOG_ADMIN,
        ),
        (Surface.REST, Surface.CHAT, Surface.MCP, Surface.WIDGET),
        "app.catalog.schemas.ProductFilters",
        "app.catalog.schemas.ProductListResponse",
        "app.catalog.service.list_products",
    ),
    _capability(
        "public.catalog.product_detail",
        "Read published product detail",
        "Read one normalized published catalog product.",
        Operation.CATALOG,
        SideEffect.READ,
        (
            Persona.SHOPPER,
            Persona.ASSOCIATE,
            Persona.MERCHANDISER,
            Persona.EXECUTIVE,
            Persona.CATALOG_ADMIN,
        ),
        (Surface.REST, Surface.CHAT, Surface.MCP, Surface.WIDGET),
        "product_id, store_id",
        "app.catalog.schemas.ProductDetailResponse",
        "app.catalog.service.get_product_detail",
    ),
    _capability(
        "public.catalog.recommendations",
        "Read catalog recommendations",
        "Return public catalog recommendation rails without customer identity.",
        Operation.CATALOG,
        SideEffect.READ,
        (
            Persona.SHOPPER,
            Persona.ASSOCIATE,
            Persona.MERCHANDISER,
            Persona.EXECUTIVE,
            Persona.CATALOG_ADMIN,
        ),
        (Surface.REST, Surface.CHAT, Surface.MCP, Surface.WIDGET),
        "app.catalog.schemas.PublicProductRecommendationRequest",
        "app.catalog.schemas.ProductRecommendationResponse",
        "app.catalog.service.recommend_products",
    ),
    _capability(
        "public.catalog.image_recommendations",
        "Read image-based catalog recommendations",
        "Analyze shopper-provided visual cues and return public catalog matches.",
        Operation.CATALOG,
        SideEffect.READ,
        (
            Persona.SHOPPER,
            Persona.ASSOCIATE,
            Persona.MERCHANDISER,
            Persona.EXECUTIVE,
            Persona.CATALOG_ADMIN,
        ),
        (Surface.REST, Surface.CHAT, Surface.WIDGET),
        "multipart image upload",
        "app.catalog.schemas.ImageRecommendationResponse",
        "app.services.image_analysis.ImageAnalysisService",
    ),
    _capability(
        "shopper.chat.turn",
        "Run storefront chat turn",
        "Handle a shopper chat turn with optional backend-derived customer identity.",
        Operation.CATALOG,
        SideEffect.READ,
        (Persona.SHOPPER, Persona.AUTHENTICATED_SHOPPER),
        (Surface.REST, Surface.CHAT, Surface.WIDGET),
        "app.services.chat.schemas.ChatRequest",
        "app.services.chat.schemas.ChatResponse",
        "app.services.chat.orchestrator.handle_chat",
    ),
    _capability(
        "shopper.account.order_status",
        "Read shopper order status",
        "Read order status for the customer linked to the authenticated shopper.",
        Operation.SHOPPER_ACCOUNT,
        SideEffect.READ,
        (Persona.AUTHENTICATED_SHOPPER,),
        (Surface.CHAT, Surface.REST),
        "backend-derived customer identity",
        "app.services.chat.schemas.ChatResponse",
        "app.services.chat.orchestrator._order_status_response",
    ),
    _capability(
        "shopper.account.recommendations",
        "Read shopper recommendations",
        "Return personalized recommendations for the authenticated shopper.",
        Operation.SHOPPER_ACCOUNT,
        SideEffect.READ,
        (Persona.AUTHENTICATED_SHOPPER,),
        (Surface.CHAT, Surface.REST),
        "backend-derived customer identity",
        "app.schemas.CustomerRecommendationResponse",
        "app.services.recommendations.customer_recommendations",
    ),
    _capability(
        "associate.customer.lookup",
        "Lookup customers",
        "Find or resolve customers for associate workflows.",
        Operation.CUSTOMER,
        SideEffect.READ,
        (Persona.ASSOCIATE,),
        (Surface.MCP, Surface.WIDGET),
        "customer query",
        "app.schemas.CustomerLookupResponse",
        "app.services.lookup.find_customers",
    ),
    _capability(
        "associate.customer.recommendations",
        "Read associate recommendations",
        "Return store-associate recommendations for a resolved customer.",
        Operation.CUSTOMER,
        SideEffect.READ,
        (Persona.ASSOCIATE,),
        (Surface.MCP, Surface.WIDGET),
        "app.schemas.CustomerRecommendationRequest",
        "app.schemas.StoreAssociateRecommendationResponse",
        "app.mcp_server._associate_recommendation_impl",
    ),
    _capability(
        "associate.customer.email.prepare",
        "Prepare customer email draft",
        "Create or update an associate-reviewed customer email draft.",
        Operation.COMMUNICATION,
        SideEffect.WRITE,
        (Persona.ASSOCIATE,),
        (Surface.MCP, Surface.WIDGET),
        "customer, store, recommendation context",
        "app.schemas.CustomerEmailDraftResponse",
        "app.services.communications.prepare_customer_email_draft",
    ),
    _capability(
        "associate.customer.email.send",
        "Send customer email draft",
        "Send a prepared customer email draft after explicit approval.",
        Operation.COMMUNICATION,
        SideEffect.SEND,
        (Persona.ASSOCIATE,),
        (Surface.MCP, Surface.WIDGET),
        "message_id, approved",
        "app.schemas.CustomerEmailSendResponse",
        "app.services.communications.send_customer_email_draft",
        approval_mode=ApprovalMode.EXPLICIT_BOOLEAN,
        approval_field="approved",
        required_grants=(Persona.SEND_CAPABLE,),
    ),
    _capability(
        "merch.strategy.override",
        "Save merchandising strategy override",
        "Save scoped merchandising strategy overrides.",
        Operation.MERCHANDISING,
        SideEffect.WRITE,
        (Persona.MERCHANDISER,),
        (Surface.MCP, Surface.WIDGET),
        "app.schemas.MerchRecommendationOverride",
        "app.schemas.MerchEffectiveStrategyResponse",
        "app.services.executive.save_merch_strategy_override",
    ),
    _capability(
        "executive.strategy.email.send",
        "Send executive strategy email",
        "Send a prepared strategy packet email after explicit approval.",
        Operation.EXECUTIVE,
        SideEffect.SEND,
        (Persona.EXECUTIVE,),
        (Surface.MCP, Surface.WIDGET),
        "packet_id, approved",
        "app.schemas.ExecutiveStrategyPacketEmailSendResponse",
        "app.services.executive.send_strategy_packet_email",
        approval_mode=ApprovalMode.EXPLICIT_BOOLEAN,
        approval_field="approved",
        required_grants=(Persona.SEND_CAPABLE,),
    ),
    _capability(
        "catalog_admin.product.draft",
        "Create catalog product draft",
        "Create or update a private Catalog Studio product draft.",
        Operation.CATALOG_ADMIN,
        SideEffect.WRITE,
        (Persona.CATALOG_ADMIN,),
        (Surface.REST, Surface.ADMIN_ASSISTANT),
        "app.catalog.admin_schemas.ProductDraftV3",
        "app.catalog.admin_schemas.AdminProductResponseV3",
        "app.services.catalog_admin.create_draft_v3",
    ),
    _capability(
        "catalog_admin.session",
        "Read Catalog Studio session",
        "Report authenticated Catalog Studio administrator status and configured capabilities.",
        Operation.CATALOG_ADMIN,
        SideEffect.READ,
        (Persona.CATALOG_ADMIN,),
        (Surface.REST, Surface.ADMIN_ASSISTANT),
        "Clerk bearer token",
        "app.routers.admin_catalog.CatalogStudioSessionResponse",
        "app.routers.admin_catalog.catalog_studio_session",
    ),
    _capability(
        "catalog_admin.assistant.query",
        "Query Catalog Studio assistant",
        "Answer bounded read-only Catalog Studio catalog and inventory questions.",
        Operation.CATALOG_ADMIN,
        SideEffect.READ,
        (Persona.CATALOG_ADMIN,),
        (Surface.REST, Surface.ADMIN_ASSISTANT),
        "app.routers.admin_catalog.CatalogAssistantQueryRequest",
        "app.services.catalog_voice_tools.CatalogVoiceToolResult",
        "app.routers.admin_catalog.query_catalog_assistant",
    ),
    _capability(
        "catalog_admin.catalog.manage",
        "Manage Catalog Studio resources",
        "Read and mutate private Catalog Studio products, workflows, source bundles, media, and review state.",
        Operation.CATALOG_ADMIN,
        SideEffect.WRITE,
        (Persona.CATALOG_ADMIN,),
        (Surface.REST, Surface.ADMIN_ASSISTANT),
        "app.catalog.admin_schemas",
        "app.catalog.admin_schemas",
        "app.routers.admin_catalog",
    ),
    _capability(
        "catalog_admin.product.publish",
        "Publish catalog product",
        "Publish a ready Catalog Studio draft into the public catalog.",
        Operation.CATALOG_ADMIN,
        SideEffect.ADMIN,
        (Persona.CATALOG_ADMIN,),
        (Surface.REST, Surface.ADMIN_ASSISTANT),
        "app.catalog.admin_schemas.PublishRequest",
        "app.catalog.admin_schemas.LifecycleMutationResponse",
        "app.services.catalog_admin.publish_draft",
    ),
    _capability(
        "operator_compatibility.admin",
        "Use legacy operator admin controls",
        "Run legacy local/operator controls that are not primary frontend contracts.",
        Operation.OPERATOR,
        SideEffect.ADMIN,
        (Persona.CATALOG_ADMIN,),
        (Surface.REST,),
        "legacy /admin request",
        "legacy operator response",
        "app.routers.admin_synthetic",
    ),
    _capability(
        "operator_compatibility.recommendations",
        "Use legacy recommendation controls",
        "Run legacy recommendation endpoints that may accept explicit customer or merchandising context.",
        Operation.OPERATOR,
        SideEffect.READ,
        (Persona.ASSOCIATE, Persona.MERCHANDISER),
        (Surface.REST,),
        "app.schemas.CustomerRecommendationRequest",
        "app.schemas.CustomerRecommendationResponse",
        "app.routers.recommendations",
    ),
    _capability(
        "operator_compatibility.product_feed",
        "Read legacy product feed",
        "Export product feed rows for compatibility integrations.",
        Operation.OPERATOR,
        SideEffect.READ,
        (Persona.MERCHANDISER, Persona.CATALOG_ADMIN),
        (Surface.REST,),
        "store_id, limit",
        "OpenAI product feed rows",
        "app.routers.recommendations.openai_product_feed",
    ),
    _capability(
        "operator_compatibility.demo_observability",
        "Use demo observability controls",
        "Toggle demo-only observability scenarios outside primary shopper contracts.",
        Operation.OPERATOR,
        SideEffect.ADMIN,
        (Persona.CATALOG_ADMIN,),
        (Surface.REST,),
        "app.schemas.DemoObservabilityUpdateRequest",
        "app.schemas.DemoObservabilityStateResponse",
        "app.routers.demo_observability",
    ),
    _capability(
        "developer_trace.read",
        "Read API traces",
        "Read authorized API trace projections and event streams.",
        Operation.TRACE,
        SideEffect.READ,
        (Persona.DEVELOPER_TRACE,),
        (Surface.REST,),
        "trace_id",
        "app.api_traces.schemas.ApiTraceProjection",
        "app.api_traces.service.get_trace_projection",
    ),
)

_CAPABILITY_BY_ID = {capability.id: capability for capability in CAPABILITIES}

_IMPLIED_PERSONAS = {
    Persona.AUTHENTICATED_SHOPPER: frozenset({Persona.SHOPPER}),
}


def list_capabilities() -> tuple[Capability, ...]:
    return CAPABILITIES


def get_capability(capability_id: str) -> Capability:
    try:
        return _CAPABILITY_BY_ID[capability_id]
    except KeyError as exc:
        raise ValueError(f"Unknown capability: {capability_id}") from exc


def expand_personas(personas: Iterable[Persona]) -> frozenset[Persona]:
    expanded = set(personas)
    for persona in tuple(expanded):
        expanded.update(_IMPLIED_PERSONAS.get(persona, ()))
    return frozenset(expanded)


def capability_allowed_for_personas(
    capability: Capability,
    personas: Iterable[Persona],
) -> bool:
    expanded = expand_personas(personas)
    return bool(expanded.intersection(capability.allowed_personas)) and set(
        capability.required_grants
    ).issubset(expanded)


def capabilities_for_personas(personas: Iterable[Persona]) -> tuple[Capability, ...]:
    return tuple(
        capability
        for capability in CAPABILITIES
        if capability_allowed_for_personas(capability, personas)
    )


def validate_capability_registry() -> None:
    seen: set[str] = set()
    for capability in CAPABILITIES:
        if capability.id in seen:
            raise ValueError(f"Duplicate capability id: {capability.id}")
        seen.add(capability.id)
        if not capability.allowed_personas:
            raise ValueError(f"{capability.id} has no allowed personas")
        if not capability.surfaces:
            raise ValueError(f"{capability.id} has no surfaces")
        if capability.side_effect == SideEffect.SEND:
            if capability.approval_mode != ApprovalMode.EXPLICIT_BOOLEAN:
                raise ValueError(f"{capability.id} must require explicit approval")
            if not capability.approval_field:
                raise ValueError(f"{capability.id} must name its approval field")
            if Persona.SEND_CAPABLE not in capability.required_grants:
                raise ValueError(f"{capability.id} must require send-capable grant")
        elif capability.approval_field and capability.approval_mode == ApprovalMode.NONE:
            raise ValueError(f"{capability.id} names an approval field but no approval mode")


validate_capability_registry()
