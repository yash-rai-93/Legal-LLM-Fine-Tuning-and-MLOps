variable "ENV" {
  type      = string
  sensitive = false
}

variable "ACCOUNT_ID" {
  type      = string
  sensitive = false
}

variable "IMAGE_TAG" {
  type      = string
  sensitive = false
}

variable "DEPLOYMENT_TIME_M" {
  type      = number
  sensitive = false
}

variable "BAKE_TIME_M" {
  type      = number
  sensitive = false
}