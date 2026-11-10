package main

import (
	"context"
	"errors"
	"net/http"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/contrib/instrumentation/runtime"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// setupOTel wires traces and metrics to an OTLP/HTTP collector. Endpoint,
// service name and resource attributes come from the standard OTEL_* env vars;
// with OTEL_EXPORTER_OTLP_ENDPOINT unset it does nothing.
func setupOTel(ctx context.Context, endpointSet bool) (func(context.Context) error, error) {
	if !endpointSet {
		return func(context.Context) error { return nil }, nil
	}
	res, err := resource.New(ctx, resource.WithFromEnv(), resource.WithTelemetrySDK(), resource.WithHost())
	if err != nil {
		return nil, err
	}
	te, err := otlptracehttp.New(ctx)
	if err != nil {
		return nil, err
	}
	me, err := otlpmetrichttp.New(ctx)
	if err != nil {
		return nil, err
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(te), sdktrace.WithResource(res))
	mp := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(me, sdkmetric.WithInterval(30*time.Second))),
		sdkmetric.WithResource(res))
	otel.SetTracerProvider(tp)
	otel.SetMeterProvider(mp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	if err := runtime.Start(runtime.WithMeterProvider(mp)); err != nil {
		return nil, err
	}
	return func(ctx context.Context) error {
		return errors.Join(tp.Shutdown(ctx), mp.Shutdown(ctx))
	}, nil
}

// instrument wraps the mux so each request gets a span named by its route
// pattern. Health checks and the long-lived SSE stream are not traced.
func instrument(mux *http.ServeMux) http.Handler {
	return otelhttp.NewHandler(mux, "bw-ui",
		otelhttp.WithFilter(func(r *http.Request) bool {
			return r.URL.Path != "/healthz" && r.URL.Path != "/api/stream"
		}),
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string {
			if _, pattern := mux.Handler(r); pattern != "" {
				return pattern
			}
			return r.Method
		}))
}
