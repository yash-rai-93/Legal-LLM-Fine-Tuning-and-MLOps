helm --namespace mlrun uninstall mlrun-ce

# Remove lingering pods
kubectl --namespace mlrun delete pods -l mlrun/class=build
kubectl --namespace mlrun delete pods -l mlrun/class=job

############################################################################################################
# https://docs.mlrun.org/en/stable/install-mlrun-ce/kubernetes-install.html#uninstalling-the-chart
# kubectl --namespace mlrun delete pod --force --grace-period=0 <pod-name>

# # To list PVCs
# $ kubectl --namespace mlrun get pvc
# ...

# # To remove a PVC
# $ kubectl --namespace mlrun delete pvc <pvc-name>
# ...

# # To list PVs
# $ kubectl --namespace mlrun get pv
# ...

# # To remove a PV
# $ kubectl --namespace mlrun delete pv <pv-name>
# ...

# aws sts get-caller-identity --region us-east-1