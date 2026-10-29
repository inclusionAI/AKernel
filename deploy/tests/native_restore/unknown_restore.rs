//! Native Restore fault qualification against the pinned ADX and Sandboxd pair.
//! Run through run.py with an empty, dedicated backend and shared filesystem paths.
#[allow(dead_code)]
#[path = "common/authority.rs"]
mod authority;
use adx_core::EnvironmentSpec;
use adxlet::sandboxd::{proto::*, Config, Sandboxd};
use hyper_util::rt::TokioIo;
use sandbox_service_client::SandboxServiceClient;
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::net::{UnixListener, UnixStream};
use tokio_stream::wrappers::UnixListenerStream;
use tonic::{
    transport::{Channel, Endpoint},
    Request, Response, Status,
};
use tower::service_fn;

#[derive(Clone)]
struct Proxy {
    client: SandboxServiceClient<Channel>,
    delay_restore: Arc<std::sync::atomic::AtomicBool>,
    fail_delete: Arc<std::sync::atomic::AtomicBool>,
    delete_failures: Arc<std::sync::atomic::AtomicUsize>,
    block_reply: Arc<std::sync::atomic::AtomicBool>,
    entered: Arc<tokio::sync::Notify>,
    release: Arc<tokio::sync::Notify>,
    request: Arc<Mutex<Option<StartRequest>>>,
}
#[tonic::async_trait]
impl sandbox_service_server::SandboxService for Proxy {
    async fn start(
        &self,
        request: Request<StartRequest>,
    ) -> Result<Response<StartResponse>, Status> {
        let request = request.into_inner();
        if request.checkpoint_info.is_some() && self.delay_restore.swap(false, Ordering::SeqCst) {
            *self.request.lock().unwrap() = Some(request);
            return Err(Status::deadline_exceeded(
                "injected Restore transport deadline",
            ));
        }
        let restoring = request.checkpoint_info.is_some();
        let response = self.client.clone().start(request).await?;
        if restoring && self.block_reply.load(Ordering::SeqCst) {
            self.entered.notify_one();
            self.release.notified().await;
        }
        Ok(response)
    }

    async fn checkpoint(
        &self,
        request: Request<CheckpointRequest>,
    ) -> Result<Response<CheckpointResponse>, Status> {
        self.client.clone().checkpoint(request).await
    }
    async fn delete(
        &self,
        request: Request<DeleteRequest>,
    ) -> Result<Response<DeleteResponse>, Status> {
        if self.fail_delete.load(Ordering::SeqCst) {
            self.delete_failures.fetch_add(1, Ordering::SeqCst);
            return Err(Status::unavailable("injected Delete failure"));
        }
        self.client.clone().delete(request).await
    }
    async fn wait(&self, request: Request<WaitRequest>) -> Result<Response<WaitResponse>, Status> {
        self.client.clone().wait(request).await
    }
    async fn list(
        &self,
        request: Request<ListSandboxesRequest>,
    ) -> Result<Response<ListSandboxesResponse>, Status> {
        self.client.clone().list(request).await
    }
    async fn stats(
        &self,
        request: Request<StatsRequest>,
    ) -> Result<Response<StatsResponse>, Status> {
        self.client.clone().stats(request).await
    }
    async fn list_available_runtimes(
        &self,
        request: Request<ListAvailableRuntimesRequest>,
    ) -> Result<Response<ListAvailableRuntimesResponse>, Status> {
        self.client.clone().list_available_runtimes(request).await
    }
    async fn set_network_policy(
        &self,
        request: Request<SetNetworkPolicyRequest>,
    ) -> Result<Response<SetNetworkPolicyResponse>, Status> {
        self.client.clone().set_network_policy(request).await
    }
}

use adx_core::{Assignment, EnvironmentRecord, EnvironmentState, Resources};
use adxlet::checkpoint::{
    CheckpointCooperation, LocalCheckpointStore, PauseRequest, ResumeRequest,
};
use adxlet::{Adxlet, Durability, Readiness, Routes, StateSink};
use std::sync::atomic::Ordering;

#[derive(Clone)]
struct Hooks(authority::Authority);
#[tonic::async_trait]
impl Readiness for Hooks {
    async fn wait_ready(&self, _: &EnvironmentRecord) -> adx_core::Result<()> {
        Ok(())
    }
}
#[tonic::async_trait]
impl Routes for Hooks {
    async fn activate(&self, _: &EnvironmentRecord) -> adx_core::Result<()> {
        Ok(())
    }
    async fn retire(&self, _: &EnvironmentRecord) -> adx_core::Result<()> {
        Ok(())
    }
}
#[tonic::async_trait]
impl StateSink for Hooks {
    async fn commit(&self, record: &EnvironmentRecord) -> adx_core::Result<Durability> {
        let mut records = self.0.records.lock().unwrap();
        records.clear();
        records.push(record.clone().try_into().unwrap());
        Ok(Durability::Published)
    }
}
#[tonic::async_trait]
impl CheckpointCooperation for Hooks {
    async fn prepare(&self, _: &EnvironmentRecord, _: &str) -> adx_core::Result<()> {
        Ok(())
    }
    async fn abort_unstarted(&self, _: &EnvironmentRecord, _: &str) -> adx_core::Result<()> {
        Ok(())
    }
    async fn resumed(&self, _: &EnvironmentRecord, _: &str) -> adx_core::Result<()> {
        Ok(())
    }
}

async fn manager(
    socket: &std::path::Path,
    root: &std::path::Path,
    auth: &authority::Fixture,
) -> Arc<Adxlet> {
    let config = Config {
        command: vec!["/bin/sh".into(), "-c".into(), "sleep 1800".into()],
        rpc_timeout: Duration::from_secs(120),
        ..Default::default()
    };
    let runtime = Sandboxd::connect(socket.to_owned(), config).await.unwrap();
    let hooks = Arc::new(Hooks(auth.authority.clone()));
    let node = Adxlet::new(
        "n".into(),
        Arc::new(runtime),
        hooks.clone(),
        hooks.clone(),
        hooks.clone(),
    )
    .with_checkpointing(
        Arc::new(LocalCheckpointStore::new(root.to_owned()).unwrap()),
        hooks,
    )
    .unwrap();
    node.update_capacity(
        Resources {
            cpu_millis: 2000,
            memory_bytes: 1 << 30,
            disk_bytes: 0,
        },
        Duration::from_secs(3600),
    )
    .unwrap();
    Arc::new(node)
}
async fn cycle(auth: &mut authority::Fixture, node: &Adxlet) {
    let result = adxlet::node_reconciliation::reconcile_node(
        &mut auth.client,
        &mut auth.registration,
        node,
        None,
        Duration::from_secs(30),
        Duration::from_secs(2),
        tokio::time::Instant::now() + Duration::from_secs(60),
    )
    .await;
    // Delete errors are retried on subsequent scans; a busy native backend can
    // also exceed the inventory budget without changing admission.
    if let Err(error) = result {
        assert!(
            error.code() == tonic::Code::Unavailable
                || (error.code() == tonic::Code::Aborted
                    && error.message() == "node authority catalog advanced"),
            "{error}"
        );
        eprintln!("retry reconciliation: {error}");
    }
    assert!(node.accepting_allocations());
}
async fn inventory(client: &mut SandboxServiceClient<Channel>) -> Vec<SandboxStatus> {
    client
        .list(ListSandboxesRequest::default())
        .await
        .unwrap()
        .into_inner()
        .sandboxes
}
async fn paused(node: &Adxlet, record: &EnvironmentRecord) {
    let current = node
        .environment(record.spec.clone(), record.assignment.clone())
        .unwrap()
        .sync()
        .await
        .unwrap();
    assert_eq!(current.durability, Durability::Published);
    assert_eq!(current.record.state, EnvironmentState::Paused);
    assert_eq!(current.record.checkpoint, record.checkpoint);
    assert!(!current.record.resources_held);
    assert_eq!(node.used(), Resources::default());
    assert!(node.accepting_allocations());
}

#[tokio::test]
#[ignore = "requires dedicated Sandboxd/runsc and identical checkpoint paths on the test host"]
async fn unknown_restore_native_converges() {
    let socket = PathBuf::from(std::env::var("ADX_SANDBOXD_TEST_SOCKET").unwrap());
    let root = PathBuf::from(std::env::var("ADX_NATIVE_STATE_DIR").unwrap());
    let channel = Endpoint::from_static("http://localhost")
        .timeout(Duration::from_secs(120))
        .connect_with_connector(service_fn(move |_| {
            let socket = socket.clone();
            async move { UnixStream::connect(socket).await.map(TokioIo::new) }
        }))
        .await
        .unwrap();
    let mut client = SandboxServiceClient::new(channel);
    assert!(
        inventory(&mut client).await.is_empty(),
        "dedicated backend must start empty"
    );
    let config = Config {
        command: vec!["/bin/sh".into(), "-c".into(), "sleep 1800".into()],
        ..Default::default()
    };
    let mut spec: EnvironmentSpec = serde_json::from_value(serde_json::json!({
        "id":"native-restore", "tenant_id":"test", "image":"ubuntu:24.04", "runtime_class":"runsc", "priority":0,
        "resources":{"cpu_millis":100,"memory_bytes":134217728,"disk_bytes":0}
    })).unwrap();
    spec.execution.rootfs = Some(adx_core::execution::Rootfs {
        readonly: false,
        source: adx_core::execution::StorageSource::Local(
            std::env::var("ADX_SANDBOXD_TEST_ROOTFS").unwrap(),
        ),
    });
    let mut sentinel =
        adxlet::sandboxd::start_request(&spec, "native-sentinel", 1, &[], &config).unwrap();
    sentinel.labels = HashMap::from([("fixture".into(), "healthy-peer".into())]);
    let sentinel = client.start(sentinel).await.unwrap().into_inner().id;
    let mut test_client = client.clone();
    let sentinel_id = sentinel.clone();
    // Always reap the fixture's physical instances, including after assertions.
    let result = tokio::spawn(async move {
        for case in ["late", "restart", "delete-retry", "resume-race"] {
            eprintln!("BEGIN native Restore {case}");
            let directory = tempfile::Builder::new().prefix(case).tempdir_in(&root).unwrap();
            let proxy_socket = directory.path().join("proxy.sock");
            let checkpoint_root = directory.path().join("checkpoints");
            let proxy = Proxy { client: test_client.clone(), delay_restore: Arc::default(), fail_delete: Arc::default(), delete_failures: Arc::default(),
                block_reply: Arc::default(), entered: Arc::default(), release: Arc::default(), request: Arc::default() };
            let listener = UnixListener::bind(&proxy_socket).unwrap();
            let service = sandbox_service_server::SandboxServiceServer::new(proxy.clone());
            let server = tokio::spawn(async move {
                tonic::transport::Server::builder().add_service(service).serve_with_incoming(UnixListenerStream::new(listener)).await.unwrap();
            });
            let mut auth = authority::Fixture::new().await;
            let mut node = manager(&proxy_socket, &checkpoint_root, &auth).await;
            cycle(&mut auth, &node).await;
            let assignment = Assignment { environment_id: spec.id.clone(), node_id: "n".into(), shard_id: 0, generation: 7, devices: vec![] };
            let handle = node.environment(spec.clone(), assignment.clone()).unwrap();
            let running = handle.create().await.unwrap().record;
            let checkpoint = handle.pause(PauseRequest { operation_id: format!("pause-{case}"), expected_revision: running.revision,
                retention: adx_core::lifecycle::RetentionPolicy::Forever, timeout_seconds: 60 }).await.unwrap().record;
            assert_eq!(checkpoint.state, EnvironmentState::Paused);
            proxy.delay_restore.store(true, Ordering::SeqCst);
            assert!(handle.resume(ResumeRequest { operation_id: format!("unknown-{case}"), expected_revision: checkpoint.revision }).await.is_err());
            let rollback = handle.sync().await.unwrap().record;
            paused(&node, &rollback).await;
            assert_eq!(rollback.checkpoint, checkpoint.checkpoint);
            assert_ne!(rollback.runtime.id, checkpoint.runtime.id);
            let request = proxy.request.lock().unwrap().clone().expect("fault must reach real Restore RPC");
            let checkpoint_path = PathBuf::from(&request.checkpoint_info.as_ref().unwrap().checkpoint_dir);
            assert!(checkpoint_path.is_dir());
            // Empty inventory after the unknown outcome is not a permanent fence.
            assert_eq!(inventory(&mut test_client).await.len(), 1);
            cycle(&mut auth, &node).await;
            drop(handle);
            if case == "restart" {
                drop(node);
                node = manager(&proxy_socket, &checkpoint_root, &auth).await;
                cycle(&mut auth, &node).await;
                paused(&node, &rollback).await;
                assert_eq!(inventory(&mut test_client).await.len(), 1);
            }
            let late = test_client.start(request).await.unwrap().into_inner().id;
            assert_ne!(late, sentinel_id);
            let mut successor = None;
            if case == "delete-retry" {
                proxy.fail_delete.store(true, Ordering::SeqCst);
                for expected in 1..=3 {
                    tokio::time::timeout(Duration::from_secs(30), async {
                        while proxy.delete_failures.load(Ordering::SeqCst) < expected {
                            cycle(&mut auth, &node).await;
                            tokio::time::sleep(Duration::from_millis(50)).await;
                        }
                    }).await.unwrap();
                    paused(&node, &rollback).await;
                    assert!(inventory(&mut test_client).await.iter().any(|s| s.id == late));
                }
                proxy.fail_delete.store(false, Ordering::SeqCst);
            }
            if case == "resume-race" {
                proxy.block_reply.store(true, Ordering::SeqCst);
                let handle = node.environment(spec.clone(), assignment.clone()).unwrap();
                let restoring = tokio::spawn(async move {
                    handle.resume(ResumeRequest { operation_id: "successful-resume".into(), expected_revision: rollback.revision }).await.unwrap().record
                });
                tokio::time::timeout(Duration::from_secs(120), proxy.entered.notified()).await.unwrap();
                assert_eq!(inventory(&mut test_client).await.len(), 3);
                for _ in 0..3 { cycle(&mut auth, &node).await; }
                assert_eq!(inventory(&mut test_client).await.len(), 3, "cleanup must queue behind active Resume");
                proxy.release.notify_one();
                successor = Some(restoring.await.unwrap());
            }
            tokio::time::timeout(Duration::from_secs(60), async {
                loop {
                    cycle(&mut auth, &node).await;
                    let actual = inventory(&mut test_client).await;
                    assert!(actual.iter().any(|s| s.id == sentinel_id && s.state == SandboxState::Running as i32), "healthy peer lost");
                    if actual.iter().all(|s| s.id != late) { break; }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            }).await.unwrap();
            let actual = inventory(&mut test_client).await;
            if let Some(running) = successor {
                assert_eq!(running.state, EnvironmentState::Running);
                assert_eq!(node.used(), running.spec.resources);
                assert_eq!(actual.len(), 2);
                assert!(actual.iter().any(|s| s.labels.get("adx.runtime_id") == Some(&running.runtime.id) && s.state == SandboxState::Running as i32));
                node.environment(spec.clone(), assignment).unwrap().delete().await.unwrap();
            } else {
                paused(&node, &rollback).await;
                assert_eq!(actual.len(), 1);
                assert!(checkpoint_path.is_dir(), "late cleanup removed retained checkpoint");
                // Restore the retained checkpoint again: proves its payload is usable.
                let handle = node.environment(spec.clone(), assignment).unwrap();
                let recovered = handle.resume(ResumeRequest { operation_id: "verify-checkpoint".into(), expected_revision: rollback.revision }).await.unwrap();
                assert_eq!(recovered.record.state, EnvironmentState::Running);
                assert_eq!(node.used(), spec.resources);
                handle.delete().await.unwrap();
            }
            assert!(node.accepting_allocations());
            assert_eq!(node.used(), Resources::default());
            assert_eq!(inventory(&mut test_client).await.len(), 1);
            server.abort();
            eprintln!("PASS native Restore {case}: orphan reclaimed, checkpoint reusable, ledger/admission/peer preserved");
        }
    }).await;
    for sandbox in inventory(&mut client).await {
        client
            .delete(DeleteRequest {
                id: sandbox.id,
                ..Default::default()
            })
            .await
            .unwrap();
    }
    assert!(
        inventory(&mut client).await.is_empty(),
        "fixture left physical runtimes"
    );
    result.unwrap();
}
